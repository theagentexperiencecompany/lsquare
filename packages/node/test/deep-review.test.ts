// Regression tests for the deep review of 0.1.1: what the app's data, its fetch, its environment and
// a polluted Object.prototype can do to the SDK. Each describe names the finding it covers. A test
// marked "compatibility guard" passed before the fixes too: it keeps a fix from changing what
// already worked. No assertion depends on elapsed time: hooks and answers wait on gates the test
// opens, and the request timeouts used are the SDK's own.
import { Readable } from 'node:stream'
import vm from 'node:vm'
import { describe, expect, it, vi } from 'vitest'
import { LSquare, type BeforeSend, type CaptureInput, type LSquareError, type LSquareOptions, type TurnEvent } from '../src/index.js'
import { jsonLength, toJson } from '../src/json.js'
import { normalize } from '../src/normalize.js'
import type { Acknowledgement } from '../../contract/src/ack.js'
import { validateEvent } from '../../contract/src/validate.js'

const silent = { info() {}, warn() {} }
const valid = (extra: Record<string, unknown> = {}) => ({ conversationId: 'c', turnId: `t${Math.random()}`, orgId: 'o', toolName: 't', question: 'q', ...extra })
/** onError and loggers run in a later event-loop turn (setImmediate), and Node reports unhandled rejections before one: let both happen. */
const tick = () => new Promise<void>((resolve) => setImmediate(() => setImmediate(resolve)))

function deferred() {
  let resolve: () => void = () => {}
  const promise = new Promise<void>((done) => (resolve = done))
  return { promise, resolve }
}

type Respond = (events: TurnEvent[], init: RequestInit) => Response | Promise<Response>
const acknowledge: Respond = (events) => new Response(JSON.stringify({ accepted: events.map((e) => e.eventId), duplicates: [], rejected: [] }))
/** Never answers; rejects when the SDK aborts the request. */
const hanging: Respond = (_events, init) => new Promise((_, reject) => init.signal?.addEventListener('abort', () => reject(new Error('aborted'))))

/** A client's options whose fetch records what it is sent and answers with `respond`, and whose onError records what it gets. */
function recorder(respond: Respond = acknowledge) {
  const sent: TurnEvent[] = []
  const errors: LSquareError[] = []
  let calls = 0
  const options: LSquareOptions = {
    apiKey: 'k',
    endpoint: 'https://x.test',
    logger: silent,
    onError: (error) => void errors.push(error),
    fetch: (async (_url: unknown, init?: RequestInit) => {
      calls++
      const { events } = JSON.parse(String(init?.body)) as { events: TurnEvent[] }
      sent.push(...events)
      return respond(events, init ?? {})
    }) as unknown as typeof fetch,
  }
  return { options, sent, errors, calls: () => calls }
}

function harness(extra: LSquareOptions = {}, respond?: Respond) {
  const recorded = recorder(respond)
  return { ...recorded, client: new LSquare({ ...recorded.options, ...extra }) }
}

/** Collects the unhandled rejections `run` leaves. */
async function unhandledDuring(run: () => unknown): Promise<unknown[]> {
  const seen: unknown[] = []
  const listener = (reason: unknown) => void seen.push(reason)
  process.on('unhandledRejection', listener)
  try {
    await run()
    await tick()
  } finally {
    process.off('unhandledRejection', listener)
  }
  return seen
}

/** Runs `run` while Object.prototype has `fields`, as a polluted one would (enumerable). */
function polluted<T>(fields: Record<string, unknown>, run: () => T): T {
  const proto = Object.prototype as Record<string, unknown>
  for (const [key, value] of Object.entries(fields)) Object.defineProperty(proto, key, { value, enumerable: true, writable: true, configurable: true })
  try {
    return run()
  } finally {
    for (const key of Object.keys(fields)) delete proto[key]
  }
}

/** polluted(), until `run`'s promise settles: the SDK reads its own objects again after capture() returns. */
async function pollutedWhile<T>(fields: Record<string, unknown>, run: () => Promise<T>): Promise<T> {
  const proto = Object.prototype as Record<string, unknown>
  for (const [key, value] of Object.entries(fields)) Object.defineProperty(proto, key, { value, enumerable: true, writable: true, configurable: true })
  try {
    return await run()
  } finally {
    for (const key of Object.keys(fields)) delete proto[key]
  }
}

const boom = () => {
  throw new Error('unavailable')
}
const throwing = <T extends object>(base: T, key: string): T => Object.defineProperty(base, key, { enumerable: true, get: boom })
const revoked = <T extends object>(target: T): T => {
  const { proxy, revoke } = Proxy.revocable(target, {})
  revoke()
  return proxy
}

describe('promises from another realm (node-legacy-1)', () => {
  // A vm context's promise isn't `instanceof` this realm's Promise; Node's own promises seen from a
  // test runner's context (Jest's) aren't either.
  const foreign = () => vm.runInNewContext('Promise.reject(new Error("secret content"))') as Promise<never>

  it('are silenced wherever the SDK reads one: fields, getters, results, metadata, toJSON, options', async () => {
    const probe = foreign()
    probe.then(undefined, () => {})
    expect(probe).not.toBeInstanceOf(Promise)
    let optionReads = 0
    let sent: TurnEvent[] = []
    const seen = await unhandledDuring(async () => {
      const recorded = recorder()
      sent = recorded.sent
      // A real getter on the options the SDK is given: read once, its promise silenced.
      const options = Object.defineProperty(recorded.options, 'maxAttempts', { enumerable: true, get: () => (optionReads++, foreign()) })
      const client = new LSquare(options)
      const getter = <T extends object>(base: T, key: string) => Object.defineProperty(base, key, { enumerable: true, get: foreign })
      client.capture(getter(valid(), 'verification'))
      client.capture(getter(valid(), 'answer'))
      client.capture(valid({ latency: getter({}, 'nested') }))
      client.capture(valid({ toolCalls: [getter({ name: 'x' }, 'result')] }))
      client.capture(valid({ toolCalls: [{ name: 'x', result: getter({}, 'deep') }] }))
      client.capture(valid({ metadata: getter({}, 'k') }))
      client.capture(valid({ verification: { toJSON: foreign } }))
      toJson(getter({}, 'a'))
      jsonLength(getter({}, 'a'), { remaining: 100 })
      const flushOptions = Object.defineProperty({}, 'timeoutMs', { enumerable: true, get: foreign })
      expect(await client.flush(flushOptions)).toMatchObject({ sent: 7 })
      await client.shutdown()
    })
    expect(seen).toEqual([])
    expect(optionReads).toBe(1)
    expect(JSON.stringify(sent)).not.toContain('secret content')
  })

  it('are told without running a Proxy trap, and silenced with the then the SDK loaded with', async () => {
    // instanceof walked a Proxy's prototype chain, running its getPrototypeOf trap.
    let traps = 0
    const answer = new Proxy({}, { getPrototypeOf: () => (traps++, Object.prototype) })
    expect(normalize(valid({ answer }))).toMatchObject({ ok: true, value: { dropped: { answer: 'wrong_type' } } })
    expect(traps).toBe(0)
    // An app (or a library it loads) that replaces Promise.prototype.then later doesn't stop it.
    const marked = Promise.reject(new Error('secret content'))
    const then = Object.getOwnPropertyDescriptor(Promise.prototype, 'then')!
    const original = Promise.prototype.then
    const seen = await unhandledDuring(() => {
      const { client } = harness()
      Promise.prototype.then = function (this: Promise<unknown>, ...args: unknown[]) {
        if (this === marked) throw new Error('patched then')
        return (original as (...rest: unknown[]) => unknown).apply(this, args)
      } as typeof original
      try {
        client.capture(Object.defineProperty(valid(), 'verification', { enumerable: true, get: () => marked }))
      } finally {
        Object.defineProperty(Promise.prototype, 'then', then)
      }
      return client.shutdown()
    })
    expect(seen).toEqual([])
  })

  it("call no other thenable's then, which may do work (compatibility guard)", async () => {
    let calls = 0
    const thenable = { then: () => void calls++ }
    const { client, sent } = harness()
    client.capture(valid({ verification: thenable, answer: thenable, metadata: { thenable } }))
    expect(await client.flush()).toMatchObject({ sent: 1 })
    expect(calls).toBe(0)
    expect(sent[0]?.dropped).toEqual({ answer: 'wrong_type', metadata: 'not_json', verification: 'not_json' })
  })

  it('are silenced when the app replaced the global Promise before loading the SDK: the then used is the native one', async () => {
    // As `global.Promise = require('bluebird')` does, and keeps: the replacement's then works on its
    // own promises, not on the native ones async functions and Node return. Read as
    // Promise.prototype.then at load, it was the replacement's (0.1.1's `instanceof Promise` missed
    // them too), and the rejection stayed unhandled.
    const Native = globalThis.Promise
    class Replacement<T> extends Native<T> {}
    Object.defineProperty(Replacement.prototype, 'then', {
      configurable: true,
      writable: true,
      value(this: unknown, ...args: unknown[]) {
        if (!(this instanceof Replacement)) throw new TypeError('not a promise of this library')
        return (Native.prototype.then as (...rest: unknown[]) => unknown).apply(this, args)
      },
    })
    let seen: unknown[] = []
    vi.resetModules()
    globalThis.Promise = Replacement as PromiseConstructor
    try {
      // A fresh copy, loaded and used while the replacement is the global.
      const fresh = await import('../src/json.js')
      const native = (async () => {
        throw new Error('secret content')
      })()
      expect(native).not.toBeInstanceOf(Replacement)
      seen = await unhandledDuring(() => fresh.silence(native))
    } finally {
      globalThis.Promise = Native
    }
    expect(seen).toEqual([])
  })
})

describe("an Array Proxy's length (node-legacy-11)", () => {
  /** Array.isArray is true for a Proxy over an array, so every array branch reads its length. */
  const sized = (length: unknown) => new Proxy([], { get: (target, key) => (key === 'length' ? length : Reflect.get(target, key)) })

  it('is read once and silenced, and one that rejects is not JSON', async () => {
    const reject = () => Promise.reject(new Error('secret content'))
    const rejecting = () => new Proxy([], { get: (target, key) => (key === 'length' ? reject() : Reflect.get(target, key)) })
    let sent: TurnEvent[] = []
    const seen = await unhandledDuring(async () => {
      const recorded = harness()
      sent = recorded.sent
      const { client } = recorded
      client.capture(valid({ verification: rejecting(), toolCalls: [{ name: 'x', result: rejecting() }] }))
      client.capture(valid({ toolCalls: rejecting() }))
      // A prototype the SDK reads to tell a plain object is the app's code too.
      client.capture(valid({ latency: new Proxy({}, { getPrototypeOf: reject }) }))
      toJson(rejecting())
      jsonLength(rejecting(), { remaining: 100 })
      expect(await client.flush()).toMatchObject({ sent: 3 })
      await client.shutdown()
    })
    expect(seen).toEqual([])
    expect(sent.map((event) => [event.verification, event.toolCalls, event.dropped])).toEqual([
      [undefined, [{ name: 'x' }], { verification: 'not_json', 'toolCalls[0].resultChars': 'not_json' }],
      // Not [], which would say no tool was called.
      [undefined, undefined, { toolCalls: 'wrong_type' }],
      [undefined, undefined, { latency: 'not_json' }],
    ])
  })

  it("is used only when it is one an array can have, an integer from 0 to 2^32 - 1", () => {
    // Before, NaN and '3' came out as arrays, with a wrong length or none, and 2^32 was walked.
    for (const length of [Number.NaN, -1, 1.5, '3', 2 ** 32]) {
      expect(toJson(sized(length), { remaining: 1_000 }), String(length)).toEqual({ ok: false, tooLarge: false })
      expect(jsonLength(sized(length), { remaining: 1_000 }), String(length)).toBe('not_json')
      expect(normalize(valid({ toolCalls: sized(length) }), { budget: 1_000 }), String(length)).toMatchObject({ ok: true, value: { dropped: { toolCalls: 'wrong_type' } } })
    }
    // The longest an array can be is a length: its slots are charged until the budget runs out.
    expect(toJson(sized(2 ** 32 - 1), { remaining: 50 })).toEqual({ ok: false, tooLarge: true })
    expect(jsonLength(sized(2 ** 32 - 1), { remaining: 50 })).toBe('too_large')
    expect(toJson(sized(0))).toEqual({ ok: true, value: [], lossy: false })
  })
})

describe('prototypes a Proxy answers with a rejected promise (node-legacy-11)', () => {
  const rejected = () => Promise.reject(new Error('secret content'))
  /** Always a rejected promise. */
  const rejecting = <T extends object>(target: T): T => new Proxy(target, { getPrototypeOf: rejected as unknown as () => object })
  /**
   * Object.prototype the first time it is asked, as the SDK's plain-object check asks; a rejected
   * promise after, as for...in asks when it walks the prototype chain, inside the engine, where
   * nothing could silence it.
   */
  const plainThenRejecting = <T extends object>(target: T): T => {
    let asked = 0
    return new Proxy(target, { getPrototypeOf: () => (asked++ === 0 ? Object.prototype : (rejected() as unknown as object)) })
  }

  it.each<[string, () => unknown, unknown]>([
    [
      "the constructor's options",
      async () => {
        const recorded = recorder()
        const client = new LSquare(rejecting(recorded.options))
        client.capture(valid())
        return client.flush()
      },
      { sent: 1 },
    ],
    [
      "flush()'s options",
      async () => {
        const { client } = harness()
        client.capture(valid())
        return client.flush(rejecting({}))
      },
      { sent: 1 },
    ],
    ["the input's own fields", () => normalize(plainThenRejecting(valid({ extra: 1 }))), { ok: true, value: { ignoredFields: { '(other)': 1 } } }],
    ['verification', () => normalize(valid({ verification: plainThenRejecting({ a: 1 }) })), { ok: true, value: { body: { verification: { a: 1 } } } }],
    [
      'a tool result being measured',
      () => normalize(valid({ toolCalls: [{ name: 'x', result: plainThenRejecting({ a: 1 }) }] })),
      { ok: true, value: { body: { toolCalls: [{ name: 'x', resultChars: 7 }] } } },
    ],
    ['metadata', () => normalize(valid({ metadata: plainThenRejecting({ a: 1 }) })), { ok: true, value: { body: { metadata: { a: 1 } } } }],
    ["a tool call's fields being counted", () => normalize(valid({ toolCalls: [plainThenRejecting({ name: 'x', tool: 'y' })] })), { ok: true, value: { ignoredToolCallFields: { tool: 1 } } }],
  ])('never leave a rejection unhandled: %s', async (_name, run, expected) => {
    let result: unknown
    const seen = await unhandledDuring(async () => {
      result = await run()
    })
    expect(seen).toEqual([])
    expect(result).toMatchObject(expected as object)
  })
})

describe("values the app's code won't let the SDK read (node-legacy-4)", () => {
  it('are treated as values of the wrong type: removed and recorded in dropped, the rest of the turn kept', () => {
    const input = valid({
      answer: 'a',
      verification: throwing({ ok: 1, list: throwing([1, 2], '1') }, 'bad'),
      latency: { get toJSON() { return boom() } },
      metadata: throwing({ kept: 'x' }, 'bad'),
      usage: throwing({ inputTokens: 1 }, 'outputTokens'),
      client: revoked({}),
      error: throwing({ type: 'E' }, 'message'),
      toolCalls: Object.defineProperty(
        [{ name: 'a', input: revoked({}), result: throwing({}, 'b') }, throwing({ name: 'b' }, 'durationMs'), new Proxy({ name: 'c' }, { ownKeys: boom })],
        3,
        { enumerable: true, get: boom },
      ),
    })
    const result = normalize(throwing(input, 'surface'))
    expect(result.ok).toBe(true)
    if (!result.ok) return
    const { body, dropped } = result.value
    expect(body).toMatchObject({ answer: 'a', metadata: { kept: 'x' }, usage: { inputTokens: 1 } })
    expect(body.verification).toEqual({ ok: 1, list: [1, null] })
    expect(body.toolCalls).toEqual([{ name: 'a' }, { name: 'b' }, { name: 'c' }])
    expect(dropped).toEqual({
      surface: 'wrong_type',
      verification: 'not_json',
      latency: 'not_json',
      metadata: 'not_json',
      'usage.outputTokens': 'wrong_type',
      client: 'wrong_type',
      error: 'wrong_type',
      'toolCalls[0].input': 'not_json',
      'toolCalls[0].resultChars': 'not_json',
      'toolCalls[1].durationMs': 'wrong_type',
      'toolCalls[3]': 'wrong_type',
    })
    expect(toJson(revoked({}))).toEqual({ ok: false, tooLarge: false })
    expect(jsonLength(revoked([]), { remaining: 100 })).toBe('not_json')
    expect(normalize(valid({ toolCalls: revoked([]) }))).toMatchObject({ ok: true, value: { dropped: { toolCalls: 'wrong_type' } } })
  })

  it("follow the wrong-type rules where those don't keep the turn or record a drop", () => {
    // A required field: the turn is dropped, as for one of the wrong type, and so is an input whose
    // prototype can't be read.
    expect(normalize(throwing(valid(), 'orgId'))).toEqual({ ok: false, code: 'missing_required', paths: ['orgId'] })
    expect(normalize(revoked(valid()))).toMatchObject({ ok: false, code: 'missing_required' })
    // A tool call's name: the call is dropped as missing_name.
    expect(normalize(valid({ toolCalls: [throwing({}, 'name'), { name: 'kept' }] }))).toMatchObject({
      ok: true,
      value: { body: { toolCalls: [{ name: 'kept' }] }, dropped: { 'toolCalls[0]': 'missing_name' } },
    })
    // Some nested optional fields are left out without a dropped entry, as a wrong-type one is.
    const nested = normalize(valid({ client: throwing({ name: 'n' }, 'version'), error: throwing({ message: 'm' }, 'type') }))
    expect(nested.ok && [nested.value.body.client, nested.value.body.error, nested.value.dropped]).toEqual([{ name: 'n' }, { message: 'm' }, {}])
  })

  it('keep a turn whose own keys, or its metadata keys, cannot be listed', () => {
    // Listing them is for the dry run's ignored fields only.
    const top = normalize(new Proxy(valid({ extra: 1 }), { ownKeys: boom }))
    expect(top.ok && [top.value.body.question, top.value.ignoredFields]).toEqual(['q', {}])
    expect(normalize(valid({ metadata: new Proxy({ a: 1 }, { ownKeys: boom }) }))).toMatchObject({ ok: true, value: { dropped: { metadata: 'not_json' } } })
  })

  it("never cost capture() its turn, nor beforeSend its event; an unreadable turnId gives a random event ID", async () => {
    const { client, sent, errors } = harness()
    client.capture(throwing(valid({ question: 'from capture', verification: throwing({}, 'bad'), latency: revoked({}) }), 'turnId'))
    await client.flush()
    await client.shutdown()
    const hooked = harness({ beforeSend: (event: TurnEvent) => throwing({ ...event, question: 'from beforeSend' }, 'verification') })
    hooked.client.capture(valid())
    await hooked.client.flush()
    await hooked.client.shutdown()
    await tick()
    expect([...sent, ...hooked.sent].map((event) => [event.question, event.dropped])).toEqual([
      ['from capture', { turnId: 'wrong_type', verification: 'not_json', latency: 'not_json' }],
      ['from beforeSend', { 'beforeSend.verification': 'not_json' }],
    ])
    // So the same turn captured twice isn't recognised as one.
    expect(sent[0]?.eventId).toMatch(/^[0-9a-f-]{36}$/)
    expect([...errors, ...hooked.errors]).toEqual([])
  })
})

describe('a polluted Object.prototype (node-legacy-4, readFields; the result-length fallback; apply())', () => {
  it('never supplies a required field or an answer the input left out', () => {
    const [missing, answered] = polluted({ orgId: 'wrong_tenant', answer: 'injected' }, () => [
      normalize({ conversationId: 'c', toolName: 't', question: 'q' }),
      normalize(valid()),
    ])
    expect(missing).toEqual({ ok: false, code: 'missing_required', paths: ['orgId'] })
    expect(answered).toMatchObject({ ok: true })
    expect(answered?.ok && answered.value.body.answer).toBeUndefined()
  })

  it("never supplies an error's message or type, while an error class's name still gives its type", () => {
    class LookupError extends Error {}
    LookupError.prototype.name = 'LookupError'
    const errors = polluted({ message: 'polluted message', type: 'polluted type' }, () =>
      [{}, { message: 'm' }, new LookupError('lookup failed'), new Error('plain')].map((error) => normalize(valid({ error }))),
    )
    expect(errors.map((result) => result.ok && [result.value.body.error, result.value.dropped])).toEqual([
      [undefined, { error: 'wrong_type' }],
      [{ message: 'm' }, {}],
      [{ message: 'lookup failed', type: 'LookupError' }, {}],
      [{ message: 'plain', type: 'Error' }, {}],
    ])
  })

  it("never stands in for a tool call's resultChars, nor supplies a result to measure", () => {
    const [measured, unmeasured] = [
      polluted({ resultChars: 7 }, () => normalize(valid({ toolCalls: [{ name: 'x', result: 'abc' }] }))),
      polluted({ result: 'xyz' }, () => normalize(valid({ toolCalls: [{ name: 'x' }] }))),
    ]
    expect(measured.ok && measured.value.body.toolCalls).toEqual([{ name: 'x', resultChars: 3 }])
    expect(unmeasured.ok && unmeasured.value.body.toolCalls).toEqual([{ name: 'x' }])
  })

  it("never stands in for an invalid status, turnId or resultChars as the field's value", () => {
    const result = polluted({ value: 'error' }, () => normalize(valid({ status: 42, turnId: {}, toolCalls: [{ name: 'x', resultChars: 'many' }] })))
    expect(result.ok).toBe(true)
    if (!result.ok) return
    expect(result.value.body.status).toBe('completed')
    expect(result.value.body.toolCalls).toEqual([{ name: 'x' }])
    expect(result.value.body.turnId).toBeUndefined()
    expect(result.value.dropped).toEqual({ status: 'wrong_type', turnId: 'wrong_type', 'toolCalls[0].resultChars': 'wrong_type' })
  })
})

describe("a polluted Object.prototype, read through the SDK's own objects (the event ID, beforeSend's identity, normalize's options, redaction, the size charge, the counts)", () => {
  /** A turn without a turnId key. */
  const withoutTurnId = (question: string) => ({ conversationId: 'c', orgId: 'o', toolName: 't', question })

  it.each([false, true])('never gives a turn without a turnId the polluted one, in its event ID or on the wire (beforeSend: %s)', async (hooked) => {
    // Inherited, it gave every such turn the same event ID, 'c/t/polluted': the ingestion keeps the
    // first and lists the rest as duplicates, which count as sent. With beforeSend, the restored
    // identity sent turnId 'polluted' too.
    const { client, sent } = harness(hooked ? { beforeSend: (event) => event } : {})
    await pollutedWhile({ turnId: 'polluted' }, async () => {
      client.capture(withoutTurnId('first'))
      client.capture(withoutTurnId('second'))
      expect(await client.flush()).toMatchObject({ sent: 2 })
    })
    expect(sent.map((event) => [event.eventId.includes('/t/'), Object.hasOwn(event, 'turnId')])).toEqual([
      [false, false],
      [false, false],
    ])
    expect(new Set(sent.map((event) => event.eventId)).size).toBe(2)
  })

  it('never adds a polluted input or errorMessage to a tool call without one, when redacting', async () => {
    const { client, sent } = harness()
    await pollutedWhile({ input: { injected: 'polluted' }, errorMessage: 'polluted' }, async () => {
      client.capture(valid({ toolCalls: [{ name: 'x' }, { name: 'y', input: { a: 1 }, errorMessage: 'e' }] }))
      expect(await client.flush()).toMatchObject({ sent: 1 })
    })
    expect(sent[0]?.toolCalls).toEqual([{ name: 'x' }, { name: 'y', input: { a: 1 }, errorMessage: 'e' }])
  })

  it('never loses the turn to a polluted error or client, which the size charge read from the body', async () => {
    // `error.message.length` and `client.name.length` threw on what the body inherited: every turn
    // without its own error or client was lost as internal_error.
    for (const fields of [{ error: 'polluted' }, { error: {} }, { client: {} }, { client: 5 }]) {
      const result = polluted(fields, () => normalize(valid()))
      expect(result.ok, JSON.stringify(fields)).toBe(true)
      expect(result.ok && [Object.hasOwn(result.value.body, 'error'), Object.hasOwn(result.value.body, 'client')]).toEqual([false, false])
    }
    const { client, sent, errors } = harness()
    polluted({ error: 'polluted' }, () => client.capture(valid()))
    expect(await client.flush()).toMatchObject({ sent: 1 })
    await tick()
    expect(errors).toEqual([])
    expect(Object.hasOwn(sent[0] ?? {}, 'error')).toBe(false)
  })

  it("never drops the turn as too_large for a long string the SDK's own error, client or tool call inherits", () => {
    // The charge read `error.type`, `client.version` and a tool call's `errorMessage` whether or not
    // they were their own: every turn whose error, client or tool call left one out was too_large.
    const long = 'x'.repeat(300_000)
    for (const key of ['type', 'version', 'errorMessage']) {
      const result = polluted({ [key]: long }, () =>
        normalize(valid({ error: { message: 'm' }, client: { name: 'n' }, toolCalls: [{ name: 'x' }] }), { budget: 256 * 1024 }),
      )
      expect(result, key).toMatchObject({ ok: true, value: { body: { error: { message: 'm' }, client: { name: 'n' }, toolCalls: [{ name: 'x' }] } } })
    }
  })

  it("never describes, in the dry-run line, tool calls, drops or redactions the event doesn't have", async () => {
    const lines: string[] = []
    const client = new LSquare({ dryRun: true, logger: { info: (line) => void lines.push(line), warn() {} } })
    await pollutedWhile({ toolCalls: 'abc', dropped: { polluted: 'not_json' }, redactions: { email: 9 } }, async () => {
      client.capture(withoutTurnId('q'))
      await client.flush()
      await tick()
    })
    expect(lines.map((line) => JSON.parse(line.replace('[lsquare dry-run] ', '')))).toEqual([
      { eventId: 'random: no turnId', fields: ['conversationId', 'orgId', 'question', 'status', 'toolName'], bytes: expect.any(Number) },
    ])
  })

  it("never takes an identity normalize() wasn't given: an incomplete turn stays rejected, and turns without a turnId keep distinct random IDs", async () => {
    // capture() passes normalize() only { budget }, and it read options.identity through
    // Object.prototype: the turn missing its orgId got one, every turn got the polluted IDs and
    // question, and all three the event ID 'attacker/t/same', which the ingestion would take for
    // duplicates. Removing the pollution before flush() didn't help: capture() had built the bodies.
    const { client, sent, errors } = harness()
    const identity = { conversationId: 'attacker', orgId: 'attacker', toolName: 'attacker', question: 'injected', turnId: 'same' }
    polluted({ identity }, () => {
      client.capture({ conversationId: 'c', toolName: 't', question: 'no org' } as unknown as CaptureInput)
      client.capture(withoutTurnId('first'))
      client.capture(withoutTurnId('second'))
    })
    expect(await client.flush()).toEqual({ sent: 2, skipped: 0, failed: 0, pending: 0 })
    await tick()
    expect(errors).toEqual([{ code: 'missing_required', message: 'Required fields are missing or not strings; the event was dropped.', paths: ['orgId'] }])
    expect(sent.map((event) => [event.conversationId, event.orgId, event.toolName, event.question, Object.hasOwn(event, 'turnId'), event.eventId.includes('/t/')])).toEqual([
      ['c', 'o', 't', 'first', false, false],
      ['c', 'o', 't', 'second', false, false],
    ])
    expect(new Set(sent.map((event) => event.eventId)).size).toBe(2)
  })

  it.each([
    ['a string', 'injected'],
    ['a number', 5],
  ])("never starts a redaction count from %s Object.prototype has: the sent event passes the ingestion's own validator", async (_kind, start) => {
    // `counts.email ?? 0` read the inherited one: 'injected' gave redactions { email: 'injected1' },
    // which the ingestion rejects as invalid_field, and 5 counted six emails for one.
    const { client, sent, errors } = harness({}, (events) => {
      // As the ingestion answers: the serialized events, checked by the contract's validator, the
      // valid ones stored and the others rejected with its reason.
      const results = events.map((event) => validateEvent(event))
      const ack: Acknowledgement = {
        accepted: results.flatMap((result) => (result.ok ? [result.event.eventId] : [])),
        duplicates: [],
        rejected: results.flatMap((result, index) =>
          result.ok ? [] : [result.eventId === undefined ? { index, reason: result.reason } : { eventId: result.eventId, reason: result.reason }],
        ),
      }
      return new Response(JSON.stringify(ack))
    })
    await pollutedWhile({ email: start }, async () => {
      client.capture(valid({ question: 'mail me at someone@example.com' }))
      expect(await client.flush()).toEqual({ sent: 1, skipped: 0, failed: 0, pending: 0 })
    })
    await tick()
    expect(errors).toEqual([])
    expect([sent[0]?.question, sent[0]?.redactions]).toEqual(['mail me at [EMAIL_1]', { email: 1 }])
    expect(validateEvent(sent[0])).toMatchObject({ ok: true })
  })

  it('never starts an ignored-field count from one Object.prototype has, in the dry-run line', async () => {
    // `counts[name] ?? 0` read the inherited one: a polluted result gave ignoredToolCallFields
    // { result: 'injected1' }, and a polluted number was added to the count.
    const lines: string[] = []
    const client = new LSquare({ dryRun: true, logger: { info: (line) => void lines.push(line), warn() {} } })
    await pollutedWhile({ result: 'injected', '(other)': 41 }, async () => {
      client.capture(valid({ custom: 1, toolCalls: [{ name: 'x', result: 'abc', extra: 2 }] }))
      await client.flush()
      await tick()
    })
    const summaries = lines.map((line) => JSON.parse(line.replace('[lsquare dry-run] ', '')))
    expect(summaries.map((summary) => [summary.ignoredFields, summary.ignoredToolCallFields])).toEqual([[{ '(other)': 1 }, { result: 1, '(other)': 1 }]])
  })
})

describe('a polluted Object.prototype.problem, while the client is made', () => {
  /** A client made while `pollute` holds: where it sends a turn, and what it warned of. */
  async function made(pollute: () => void, endpoint: string) {
    const urls: string[] = []
    const warned: string[] = []
    const fetch = (async (url: unknown, init?: RequestInit) => {
      urls.push(String(url))
      return acknowledge((JSON.parse(String(init?.body)) as { events: TurnEvent[] }).events, init ?? {})
    }) as unknown as typeof globalThis.fetch
    pollute()
    let client: LSquare
    try {
      client = new LSquare({ apiKey: 'k', endpoint, fetch, logger: { info() {}, warn: (line) => void warned.push(line) } })
    } finally {
      delete (Object.prototype as Record<string, unknown>).problem
    }
    client.capture(valid())
    await client.flush()
    await tick()
    return { urls, warned }
  }

  it.each(['a data property', 'a getter'])('as %s, never turns capture off; an invalid endpoint still does', async (kind) => {
    let reads = 0
    const pollute = () =>
      kind === 'a getter'
        ? Object.defineProperty(Object.prototype, 'problem', { get: () => (reads++, 'polluted'), enumerable: true, configurable: true })
        : Object.defineProperty(Object.prototype, 'problem', { value: 'polluted', enumerable: true, writable: true, configurable: true })
    expect(await made(pollute, 'https://x.test')).toEqual({ urls: ['https://x.test/v1/turns'], warned: [] })
    expect(await made(pollute, 'http://x.test')).toEqual({ urls: [], warned: ['[lsquare] The endpoint must use https (http only for localhost): capture is disabled.'] })
    expect(reads).toBe(0)
  })
})

describe('a response body, from a fetch that ignores the abort (node-legacy-2)', () => {
  /** A 2xx answer whose web-stream body sends `chunk`, if any, then stalls: it says when it is pulled after that, and whether it was cancelled. */
  function stalledBody(chunk?: string) {
    const state = { stalled: deferred(), cancelled: false }
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        if (chunk !== undefined) controller.enqueue(new TextEncoder().encode(chunk))
      },
      pull() {
        state.stalled.resolve()
        return new Promise<void>(() => {})
      },
      cancel() {
        state.cancelled = true
      },
    })
    return { state, response: new Response(stream, { status: 200 }) }
  }
  /** A 2xx answer with a Node stream body, as some fetch polyfills return. */
  const nodeAnswer = (body: Readable, headers: unknown = new Headers()) => ({ status: 200, headers, body }) as unknown as Response

  it("is cancelled at the request's timeout, mid-body, and the attempt is retried as a network error", async () => {
    const bodies: Array<{ cancelled: boolean }> = []
    const input = valid()
    const { client, errors, calls } = harness({ requestTimeoutMs: 20, maxAttempts: 2 }, () => {
      const answer = stalledBody('{"accepted":[')
      bodies.push(answer.state)
      return answer.response
    })
    // No backoff between the two attempts.
    const random = vi.spyOn(Math, 'random').mockReturnValue(0)
    try {
      client.capture(input)
      expect(await client.flush({ timeoutMs: 10_000 })).toMatchObject({ failed: 1, pending: 0 })
    } finally {
      random.mockRestore()
    }
    await tick()
    expect(calls()).toBe(2)
    expect(bodies.map((body) => body.cancelled)).toEqual([true, true])
    // A read the cancel ended early isn't taken as the answer: 'unexpected' would not be retried.
    expect(errors.map((error) => error.message)).toEqual(['Network error after 2 attempts.'])
  })

  it('is cancelled when shutdown() drops its event, long before the timeout', async () => {
    const answer = stalledBody('{"accepted":[')
    const { client } = harness({ requestTimeoutMs: 60_000 }, () => answer.response)
    client.capture(valid())
    // Pulled again once the SDK has read the first chunk: it is waiting on the body.
    await answer.state.stalled.promise
    expect(answer.state.cancelled).toBe(false)
    expect(await client.shutdown({ timeoutMs: 0 })).toMatchObject({ failed: 1 })
    expect(answer.state.cancelled).toBe(true)
  })

  it("as a Node stream being read, is destroyed at the request's timeout", async () => {
    const body = new Readable({ read() {} })
    const { client, errors } = harness({ requestTimeoutMs: 20, maxAttempts: 1 }, () => nodeAnswer(body))
    client.capture(valid())
    expect(await client.flush({ timeoutMs: 10_000 })).toMatchObject({ failed: 1, pending: 0 })
    expect(body.destroyed).toBe(true)
    await tick()
    expect(errors.map((error) => error.message)).toEqual(['Network error after 1 attempts.'])
  })

  it.each(['web stream', 'Node stream'])("is stopped when its response comes only after the request's timeout: a %s", async (kind) => {
    const answered = deferred()
    let stopped = () => false
    let node: Readable | undefined
    const { client, errors } = harness({ requestTimeoutMs: 20, maxAttempts: 1 }, async () => {
      await answered.promise
      if (kind === 'Node stream') {
        node = new Readable({ read() {} })
        const body = node
        stopped = () => body.destroyed
        return nodeAnswer(body)
      }
      const answer = stalledBody()
      stopped = () => answer.state.cancelled
      return answer.response
    })
    client.capture(valid())
    expect(await client.flush({ timeoutMs: 10_000 })).toMatchObject({ failed: 1, pending: 0 })
    await tick()
    expect(errors.map((error) => error.code)).toEqual(['send_failed'])
    // Answers only now, after its event failed.
    answered.resolve()
    await tick()
    expect(stopped()).toBe(true)
    // Its socket resets later: with no 'error' listener, Node would throw that into the app's process.
    if (node) expect(() => node?.emit('error', new Error('socket reset'))).not.toThrow()
  })

  it('is destroyed when handling its response throws before the body is read, and its later errors never reach the app', async () => {
    const body = new Readable({ read() {} })
    body.push('{"accepted":[],"duplicates":[],"rejected":[]}')
    const headers = { get: boom }
    const { client, errors } = harness({ maxAttempts: 1 }, () => nodeAnswer(body, headers))
    client.capture(valid())
    expect(await client.flush()).toMatchObject({ failed: 1 })
    await tick()
    expect(errors.map((error) => error.message)).toEqual(['Network error after 1 attempts.'])
    expect(body.destroyed).toBe(true)
    expect(() => body.emit('error', new Error('socket reset'))).not.toThrow()
  })
})

describe("dry run's default logger (node-legacy-6)", () => {
  const names = ['log', 'info', 'debug', 'warn', 'error'] as const
  /** What each console method was called with while `run` runs; a dry-run line by its prefix. */
  async function consoleDuring(run: () => Promise<void>) {
    const spies = names.map((name) => vi.spyOn(console, name).mockImplementation(() => {}))
    const line = (value: unknown) => (String(value).startsWith('[lsquare dry-run] ') ? '[lsquare dry-run] ' : String(value))
    try {
      await run()
      await tick()
      return Object.fromEntries(names.map((name, i) => [name, spies[i]!.mock.calls.map(([value]) => line(value))]))
    } finally {
      for (const spy of spies) spy.mockRestore()
    }
  }

  it("writes to stderr, never to stdout, which carries a stdio server's MCP messages", async () => {
    const called = await consoleDuring(async () => {
      const client = new LSquare({ dryRun: true })
      client.capture(valid())
      expect(await client.flush()).toMatchObject({ skipped: 1 })
    })
    // console.log, info and debug write to stdout; warn and error to stderr.
    expect(called).toEqual({ log: [], info: [], debug: [], warn: [], error: ['[lsquare dry-run] '] })
  })

  it('leaves warnings on console.warn, and console passed explicitly as it is (compatibility guard)', async () => {
    const called = await consoleDuring(async () => {
      void new LSquare({ apiKey: undefined })
      const client = new LSquare({ dryRun: true, logger: console })
      client.capture(valid())
      expect(await client.flush()).toMatchObject({ skipped: 1 })
    })
    expect(called).toEqual({ log: [], info: ['[lsquare dry-run] '], debug: [], warn: ['[lsquare] No apiKey: capture is disabled.'], error: [] })
  })
})

describe('a timeoutMs that throws when read (node-legacy-8)', () => {
  const unreadable = Object.defineProperty({}, 'timeoutMs', { enumerable: true, get: boom }) as { timeoutMs?: number }

  it('counts as not given: shutdown() still drops an event waiting for beforeSend, which is never sent', async () => {
    const started = deferred()
    const release = deferred()
    const returned = deferred()
    const beforeSend: BeforeSend = async (event) => {
      started.resolve()
      await release.promise
      returned.resolve()
      return event
    }
    const { client, errors, calls } = harness({ beforeSend, beforeSendTimeoutMs: 60_000 })
    client.capture(valid())
    await started.promise
    expect(await client.shutdown(unreadable)).toEqual({ sent: 0, skipped: 0, failed: 1, pending: 0 })
    release.resolve()
    await returned.promise
    await tick()
    expect(calls()).toBe(0)
    expect(errors.map((error) => error.code)).toEqual(['shutdown'])
  })

  it("counts as not given: flush() waits its default, then reports what is pending (a Proxy whose get trap throws)", async () => {
    const options = new Proxy({ timeoutMs: 0 }, { get: boom })
    const { client, errors } = harness({ flushTimeoutMs: 20 }, hanging)
    client.capture(valid())
    expect(await client.flush(options)).toEqual({ sent: 0, skipped: 0, failed: 0, pending: 1 })
    await tick()
    expect(errors.map((error) => error.code)).toEqual(['pending_after_flush'])
    await client.shutdown({ timeoutMs: 0 })
  })
})

describe("objects from another realm, as in a test runner's vm context (node-legacy-10)", () => {
  it('beforeSend gets an event of this realm, whatever the global structuredClone is', async () => {
    // jest-environment-node gives its context the outer realm's structuredClone, so a copy it makes
    // has another Object.prototype.
    const real = globalThis.structuredClone
    const recorded = recorder()
    const hooks: BeforeSend[] = [(event) => event, (event) => ({ ...event, question: 'scrubbed' })]
    globalThis.structuredClone = vm.runInNewContext('(value) => JSON.parse(JSON.stringify(value))') as typeof structuredClone
    try {
      for (const beforeSend of hooks) {
        const client = new LSquare({ ...recorded.options, beforeSend })
        client.capture(valid({ toolCalls: [{ name: 'find', input: { x: 1 } }], usage: { inputTokens: 1 }, latency: { totalMs: 3 }, client: { name: 'claude' } }))
        expect(await client.flush()).toMatchObject({ sent: 1, failed: 0 })
      }
    } finally {
      globalThis.structuredClone = real
    }
    expect(recorded.errors).toEqual([])
    expect(recorded.sent.map(({ question, toolCalls, usage, latency, client, dropped }) => ({ question, toolCalls, usage, latency, client, dropped }))).toEqual(
      ['q', 'scrubbed'].map((question) => ({
        question,
        toolCalls: [{ name: 'find', input: { x: 1 } }],
        usage: { inputTokens: 1 },
        latency: { totalMs: 3 },
        client: { name: 'claude' },
        dropped: undefined,
      })),
    )
  })

  it('reads an acknowledgement whose chunks are Uint8Arrays of another realm', async () => {
    const foreign = vm.runInNewContext('(text) => Uint8Array.from(text, (c) => c.charCodeAt(0))') as (text: string) => Uint8Array
    expect(foreign('x')).not.toBeInstanceOf(Uint8Array)
    const { client, errors } = harness({}, (events) => {
      const chunks = [foreign(JSON.stringify({ accepted: events.map((e) => e.eventId), duplicates: [], rejected: [] }))]
      const reader = { read: async () => (chunks.length > 0 ? { done: false, value: chunks.shift() } : { done: true, value: undefined }), cancel: async () => {} }
      return { status: 200, headers: new Headers(), body: { getReader: () => reader } } as unknown as Response
    })
    client.capture(valid())
    expect(await client.flush()).toMatchObject({ sent: 1, failed: 0 })
    await tick()
    expect(errors).toEqual([])
  })

  it("never hands the hook what an inherited toJSON makes of the event (compatibility guard)", async () => {
    // A copy through JSON would run a polluted Object.prototype.toJSON: this one copies the private
    // question into the answer, so a hook that scrubs the question would send it anyway.
    const PRIVATE = 'private question'
    const proto = Object.prototype as Record<string, unknown>
    const { client, sent, errors } = harness({ beforeSend: (event) => ({ ...event, question: 'scrubbed' }) })
    Object.defineProperty(proto, 'toJSON', {
      value(this: Record<string, unknown>) {
        return Object.prototype.hasOwnProperty.call(this, 'eventId') && this.question === PRIVATE ? { ...this, answer: this.question } : this
      },
      configurable: true,
      writable: true,
    })
    try {
      client.capture(valid({ question: PRIVATE }))
      expect(await client.flush()).toMatchObject({ sent: 1 })
    } finally {
      delete proto.toJSON
    }
    expect(errors).toEqual([])
    expect(sent.map(({ question, answer }) => ({ question, answer }))).toEqual([{ question: 'scrubbed', answer: undefined }])
    expect(JSON.stringify(sent)).not.toContain(PRIVATE)
  })

  it('hands the hook -0 as -0, as structuredClone did (compatibility guard)', async () => {
    const seen: unknown[] = []
    const { client } = harness({ beforeSend: (event) => (seen.push(event.usage?.inputTokens), null) })
    client.capture(valid({ usage: { inputTokens: -0 } }))
    expect(await client.flush()).toMatchObject({ skipped: 1 })
    expect(Object.is(seen[0], -0)).toBe(true)
  })

  it('copies the event without defining its properties one at a time', async () => {
    // An Object.defineProperty call with a new descriptor per key and slot cost an object-heavy event
    // about 0.5 ms more than the structuredClone it replaced, on the app's event loop.
    const keys = Array.from({ length: 500 }, (_, i) => `copied${i}`)
    let seen: TurnEvent | undefined
    const { client } = harness({ beforeSend: (event) => ((seen = event), null) })
    const defineProperty = vi.spyOn(Object, 'defineProperty')
    let defined: number
    try {
      client.capture(valid({ metadata: Object.fromEntries(keys.map((key) => [key, 1])) }))
      expect(await client.flush()).toMatchObject({ skipped: 1 })
      defined = defineProperty.mock.calls.filter(([, key]) => typeof key === 'string' && key.startsWith('copied')).length
    } finally {
      defineProperty.mockRestore()
    }
    expect(defined).toBe(0)
    expect(Object.keys(seen?.metadata ?? {})).toEqual(keys)
  })

  it("defines the copy's properties, so an inherited setter never runs nor takes the hook's fields", async () => {
    // A guard on the copy's design: assigned, `copy.question = ...` would run a setter on
    // Object.prototype, which would get the question, and the hook an event without one. redact:
    // false, so only the copy could assign. (0.1.1 lost this turn before the hook: its readFields
    // assigned to an ordinary object.)
    const proto = Object.prototype as Record<string, unknown>
    const setterGot: unknown[] = []
    let hookGot: unknown
    const { client, sent } = harness({ redact: false, beforeSend: (event) => ((hookGot = Object.hasOwn(event, 'question') && event.question), event) })
    Object.defineProperty(proto, 'question', { configurable: true, get: () => undefined, set: (value) => void setterGot.push(value) })
    try {
      client.capture(valid({ question: 'private question' }))
      expect(await client.flush()).toMatchObject({ sent: 1 })
    } finally {
      delete proto.question
    }
    expect(setterGot).toEqual([])
    expect(hookGot).toBe('private question')
    expect(sent[0]?.question).toBe('private question')
  })
})

describe("tool results' own work limit (the size-limit docs)", () => {
  it('measures a result against its own 1,000,000 units per event, whatever maxEventBytes is (documents behaviour)', async () => {
    // A docs fix: this passed before. The size-limit docs named only key listing and the app's code
    // as outside the event size limit's charge; measuring results is outside it too.
    const { client, sent } = harness({ maxEventBytes: 2_000 })
    client.capture(valid({ toolCalls: [{ name: 'x', result: new Array(450_000).fill(0) }] }))
    client.capture(valid({ toolCalls: [{ name: 'x', result: new Array(1_100_000).fill(0) }] }))
    expect(await client.flush()).toMatchObject({ sent: 2, failed: 0 })
    expect(sent.map((event) => [event.toolCalls, event.dropped])).toEqual([
      [[{ name: 'x', resultChars: 900_001 }], undefined],
      [[{ name: 'x' }], { 'toolCalls[0].resultChars': 'too_large' }],
    ])
  })
})

describe('a rejection by position (node-legacy-12)', () => {
  const answering = (body: Record<string, unknown>) => harness({}, () => new Response(JSON.stringify(body)))

  it("at index 0 without an eventId is this event's, the one it carries: reported as rejected", async () => {
    // A NUL in the turn ID gives an eventId the ingestion can't use: it names the event by index.
    for (const [reason, said] of [
      ['invalid_field', 'invalid_field'],
      ['made_up', 'unspecified'],
    ]) {
      const { client, sent, errors } = answering({ accepted: [], duplicates: [], rejected: [{ index: 0, reason }] })
      client.capture(valid({ turnId: 'turn\u0000one' }))
      expect(await client.flush()).toEqual({ sent: 0, skipped: 0, failed: 1, pending: 0 })
      await tick()
      expect(sent.map((event) => event.eventId)).toEqual(['c/t/turn\u0000one'])
      expect(errors).toEqual([{ eventId: 'c/t/turn\u0000one', code: 'rejected', message: `LSquare rejected the event: ${said}.` }])
    }
  })

  it('elsewhere, or naming another event, is not; listed with its acceptance, the answer is unexpected', async () => {
    const outcome = async (ack: (eventId: string) => Record<string, unknown>) => {
      let eventId = ''
      const { client, errors } = harness({}, (events) => {
        eventId = events[0]!.eventId
        return new Response(JSON.stringify(ack(eventId)))
      })
      client.capture(valid())
      const result = await client.flush()
      await tick()
      return [result.sent, errors.map((error) => error.code)]
    }
    expect(await outcome(() => ({ accepted: [], rejected: [{ index: 1, reason: 'invalid_field' }] }))).toEqual([0, ['send_failed']])
    expect(await outcome(() => ({ accepted: [], rejected: [{ eventId: 'other', index: 0, reason: 'invalid_field' }] }))).toEqual([0, ['send_failed']])
    expect(await outcome((id) => ({ accepted: [id], rejected: [{ eventId: 'other', index: 0, reason: 'invalid_field' }] }))).toEqual([1, []])
    expect(await outcome((id) => ({ accepted: [id], rejected: [{ index: 0, reason: 'invalid_field' }] }))).toEqual([0, ['send_failed']])
  })
})

describe("dry run's event ID label (node-legacy-13)", () => {
  it("says an ID came from turnId, unhashed, when the conversation ID is 'h'", async () => {
    // Unhashed, that ID is 'h/t/1': it starts as a hashed one does ('h/' and hex).
    const lines: string[] = []
    const client = new LSquare({ dryRun: true, logger: { info: (line) => void lines.push(line), warn() {} } })
    client.capture(valid({ conversationId: 'h', turnId: '1' }))
    client.capture(valid({ conversationId: 'h', turnId: 'x'.repeat(600) }))
    client.capture(valid({ conversationId: 'h', turnId: undefined }))
    await client.flush()
    await tick()
    expect(lines.map((line) => JSON.parse(line.replace('[lsquare dry-run] ', '')).eventId)).toEqual([
      'from turnId',
      'from turnId, hashed',
      'random: no turnId',
    ])
  })
})
