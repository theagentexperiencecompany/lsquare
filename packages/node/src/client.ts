import { AsyncLocalStorage, AsyncResource } from 'node:async_hooks'
import { types } from 'node:util'
import { hasOwn, isPlainObject, readSettings } from './json.js'
import { eventIdFor, normalize, SDK_FIELDS, type TurnBody } from './normalize.js'
import { redactBody, resolvePatterns } from './redact.js'
import type {
  BeforeSend,
  CaptureInput,
  DroppedReason,
  FlushResult,
  Logger,
  LSquareError,
  LSquareOptions,
  PatternName,
  TurnEvent,
} from './types.js'

export const SDK_NAME = '@lsquare/node'
export const SDK_VERSION = '0.1.2'

const DEFAULTS = {
  flushTimeoutMs: 2_000,
  maxConcurrentHooks: 16,
  maxConcurrentRequests: 32,
  maxQueueEvents: 1_000,
  maxQueueBytes: 10 * 1024 * 1024,
  maxEventBytes: 256 * 1024,
  maxAttempts: 3,
  requestTimeoutMs: 10_000,
}
/** Every option, so the compiler fails if one is added to LSquareOptions but never read. */
const OPTION_KEYS: Record<keyof LSquareOptions, true> = {
  apiKey: true,
  endpoint: true,
  dryRun: true,
  flushTimeoutMs: true,
  beforeSend: true,
  beforeSendTimeoutMs: true,
  maxConcurrentHooks: true,
  maxConcurrentRequests: true,
  redact: true,
  onError: true,
  logger: true,
  maxQueueEvents: true,
  maxQueueBytes: true,
  maxEventBytes: true,
  maxAttempts: true,
  requestTimeoutMs: true,
  fetch: true,
}
const RETRY_BASE_MS = 250
const RETRY_CAP_MS = 5_000
const RETRY_AFTER_CAP_MS = 30_000
const ERROR_LOG_INTERVAL_MS = 60_000
/**
 * Callback nesting. The app's own code runs at depth 0; an `onError` or logger call delivered at depth n
 * runs at depth n, and so does everything it starts, across awaits, timers and microtasks. Events
 * remember the depth they were captured at. Reports and logs deeper than MAX_CALLBACK_DEPTH are
 * dropped, so no callback can feed itself, synchronously or not. Shared by all clients, so loops
 * between two clients are cut too.
 *
 * Only the app's own callbacks run inside it. Before Node 24, an AsyncLocalStorage in use costs
 * every promise in the process, so the default logger, which can't call back, stays outside.
 */
const callbackDepth = new AsyncLocalStorage<number>()
const MAX_CALLBACK_DEPTH = 2
const currentDepth = () => callbackDepth.getStore() ?? 0

/**
 * Backstop for loops the depth can't see (work handed to a queue or promise set up outside the
 * callback): callbacks run in a later event-loop turn, so no loop can starve timers or I/O, and at
 * most this many per second per client, so any loop dies out.
 */
const CALLBACKS_PER_SECOND = 100

class RateLimit {
  private tokens = CALLBACKS_PER_SECOND
  private last = performance.now()
  suppressed = 0

  take(): boolean {
    const now = performance.now()
    this.tokens = Math.min(CALLBACKS_PER_SECOND, this.tokens + ((now - this.last) / 1000) * CALLBACKS_PER_SECOND)
    this.last = now
    if (this.tokens < 1) {
      this.suppressed++
      return false
    }
    this.tokens -= 1
    return true
  }

  /** How many were dropped since the last call, resetting the count. */
  drain(): number {
    const count = this.suppressed
    this.suppressed = 0
    return count
  }
}
/**
 * The default logger: `console`, with both levels on standard error. `console.info` writes to
 * stdout, which carries a stdio server's MCP messages: a dry-run line there would corrupt them.
 * Like `console`, it never calls back into the SDK. `logger: console`, passed explicitly, is used
 * as given.
 */
const DEFAULT_LOGGER: Logger = {
  info: (message) => console.error(message),
  warn: (message) => console.warn(message),
}
const MAX_RESPONSE_BYTES = 64 * 1024
const LOCAL_HOSTS = new Set(['localhost', '127.0.0.1', '[::1]'])
/** Where events go unless the `endpoint` option or LSQUARE_ENDPOINT says otherwise. */
export const DEFAULT_ENDPOINT = 'https://ingest.lsquare.io'
/** Reasons the ingestion service may give. Anything else is reported as "unspecified". */
const REJECTION_REASONS = new Set(['missing_field', 'invalid_field', 'invalid_conversation_id', 'too_large', 'unsupported_schema_version'])

type Outcome = 'sent' | 'skipped' | 'failed'
/** `queued`: ready to send, waiting for a request slot. */
type Stage = 'waiting' | 'hook' | 'ready' | 'queued' | 'sending' | 'retry'

interface Entry {
  seq: number
  /** Callback depth at capture: reports about this event are one deeper. */
  depth: number
  /**
   * The async context of the `capture()` call. The event's hook, requests, reports and logs run in
   * it, so the app's own AsyncLocalStorage (request, tenant, trace) is that of the request that
   * captured it. Released (undefined) once the entry is finished, like `event`: it keeps the app's
   * stores for that request reachable.
   */
  context: AsyncResource | undefined
  /** performance.now() at capture: monotonic, for deadlines. */
  createdAt: number
  eventId: string
  /** Released (undefined) once the entry is finished, so nothing still holding the entry holds content. */
  event: TurnEvent | undefined
  /** The normalised body awaiting redaction when there is no beforeSend; released like `event`. */
  body: TurnBody | undefined
  dropped: Record<string, DroppedReason>
  bytes: number
  stage: Stage
  redacted: boolean
  attempts: number
  /** performance.now() of the next attempt while waiting to retry. */
  retryAt: number | undefined
  outcome: Outcome | undefined
  /** Finished while its hook was still running: keeps its slot and bytes until the hook settles. */
  zombie: boolean
  hookAbort: AbortController | undefined
  fetchAbort: AbortController | undefined
  timer: ReturnType<typeof setTimeout> | undefined
  ignoredFields: Record<string, number>
  ignoredToolCallFields: Record<string, number>
}

/** A `flush()` or `shutdown()` waiting for events, until each is finished or can't be by `deadline`. */
interface Wait {
  deadline: number
  pending: Set<Entry>
  end: () => void
}

export class LSquare {
  // True private fields (#), not TypeScript's `private`: nothing outside the class can read them,
  // so printing or serialising a client (console.log, JSON.stringify, error reporters) never shows
  // the key or the content of queued events.
  #mode: 'disabled' | 'dry-run' | 'send' = 'disabled'
  #apiKey = ''
  #url = ''
  #fetchImpl: typeof fetch | undefined
  #beforeSend: BeforeSend | undefined
  #patterns: PatternName[] = []
  #onErrorHandler: LSquareOptions['onError']
  #logger: Logger = DEFAULT_LOGGER
  /** The app's own logger: its calls are callbacks, run at a depth. */
  #customLogger = false
  #flushTimeoutMs = DEFAULTS.flushTimeoutMs
  #beforeSendTimeoutMs = DEFAULTS.flushTimeoutMs
  #maxConcurrentHooks = DEFAULTS.maxConcurrentHooks
  #maxConcurrentRequests = DEFAULTS.maxConcurrentRequests
  #maxQueueEvents = DEFAULTS.maxQueueEvents
  #maxQueueBytes = DEFAULTS.maxQueueBytes
  #maxEventBytes = DEFAULTS.maxEventBytes
  #maxAttempts = DEFAULTS.maxAttempts
  #requestTimeoutMs = DEFAULTS.requestTimeoutMs

  readonly #live = new Map<number, Entry>()
  #liveBytes = 0
  #zombies = 0
  #zombieBytes = 0
  #activeHooks = 0
  #activeRequests = 0
  /** Events waiting for a request slot, in the order they became ready. */
  readonly #queued = new Set<Entry>()
  readonly #waits = new Set<Wait>()
  #seq = 0
  #closed = false
  #reportedClosed = false
  readonly #reportLimit = new RateLimit()
  readonly #logLimit = new RateLimit()
  #lastErrorLog = Number.NEGATIVE_INFINITY
  #suppressedErrors = 0

  /** Never throws. Without an `apiKey`, or with an invalid endpoint (outside dry run), the client is disabled. */
  constructor(options: LSquareOptions = {}) {
    try {
      // Read once each, never from Object.prototype: a polluted prototype can't supply an endpoint,
      // a key or `redact: false`. A config class's getters still work.
      const opts = readSettings(options, Object.keys(OPTION_KEYS)) as LSquareOptions
      if (opts.logger) {
        this.#logger = opts.logger
        this.#customLogger = opts.logger !== console
      }
      this.#onErrorHandler = typeof opts.onError === 'function' ? opts.onError : undefined
      this.#flushTimeoutMs = waitTime(opts.flushTimeoutMs, DEFAULTS.flushTimeoutMs)
      // When flush() doesn't wait (0), a hook still gets the default time.
      this.#beforeSendTimeoutMs = duration(opts.beforeSendTimeoutMs, this.#flushTimeoutMs || DEFAULTS.flushTimeoutMs)
      this.#maxConcurrentHooks = positiveInt(opts.maxConcurrentHooks, DEFAULTS.maxConcurrentHooks)
      this.#maxConcurrentRequests = positiveInt(opts.maxConcurrentRequests, DEFAULTS.maxConcurrentRequests)
      this.#maxQueueEvents = positiveInt(opts.maxQueueEvents, DEFAULTS.maxQueueEvents)
      this.#maxQueueBytes = positiveInt(opts.maxQueueBytes, DEFAULTS.maxQueueBytes)
      this.#maxEventBytes = positiveInt(opts.maxEventBytes, DEFAULTS.maxEventBytes)
      this.#maxAttempts = positiveInt(opts.maxAttempts, DEFAULTS.maxAttempts)
      this.#requestTimeoutMs = duration(opts.requestTimeoutMs, DEFAULTS.requestTimeoutMs)
      this.#beforeSend = typeof opts.beforeSend === 'function' ? opts.beforeSend : undefined
      this.#patterns = resolvePatterns(opts.redact)
      this.#fetchImpl = opts.fetch ?? globalThis.fetch
      const apiKey = typeof opts.apiKey === 'string' ? opts.apiKey.trim() : ''
      const dryRun = opts.dryRun ?? ['1', 'true'].includes(env('LSQUARE_DRY_RUN') ?? '')

      if (dryRun) {
        this.#mode = 'dry-run'
        return
      }
      if (!apiKey) {
        this.log('warn', '[lsquare] No apiKey: capture is disabled.')
        return
      }
      if (typeof this.#fetchImpl !== 'function') {
        this.log('warn', '[lsquare] fetch is not available in this runtime: capture is disabled.')
        return
      }
      const endpoint = resolveEndpoint(opts.endpoint || env('LSQUARE_ENDPOINT') || DEFAULT_ENDPOINT)
      // Its own: `in` would find a polluted Object.prototype's, and turn capture off.
      if (hasOwn(endpoint, 'problem')) {
        this.log('warn', `[lsquare] ${(endpoint as { problem: string }).problem}: capture is disabled.`)
        return
      }
      this.#mode = 'send'
      this.#url = (endpoint as { url: string }).url
      this.#apiKey = apiKey
    } catch {
      this.#mode = 'disabled'
      this.log('warn', '[lsquare] Invalid options: capture is disabled.')
    }
  }

  /**
   * Never throws and never waits on the network. Its synchronous work is charged against the event
   * size limit, except three things: measuring non-string tool results, which are never sent and
   * have their own limit of 1,000,000 units per event; listing the keys of the input's objects; and
   * running code supplied with the input (getters, `toJSON`, Proxy traps). No limit covers the last
   * two. Call it as a method: detached from its client (e.g. `.then(lsquare.capture)`), it drops
   * the event, with nowhere to report it.
   */
  capture(input: CaptureInput): void {
    try {
      this.enqueue(input)
    } catch {
      try {
        this.report({ code: 'internal_error', message: 'capture() failed unexpectedly; the event was dropped.' })
      } catch {
        // Called detached, `this` is undefined. Throwing here would crash the app from a `.then()`.
      }
    }
  }

  /**
   * Waits up to `timeoutMs` for the events captured before the call, except those whose next retry
   * is after the deadline: they can't finish in time. `0` doesn't wait; a `timeoutMs` that can't be
   * read (a getter that throws) counts as not given. Never throws or rejects. Events still pending
   * when it returns are reported to `onError`, unless `timeoutMs` is `0`.
   */
  async flush(options?: { timeoutMs?: number | undefined }): Promise<FlushResult> {
    const result: FlushResult = { sent: 0, skipped: 0, failed: 0, pending: 0 }
    try {
      if (this.#mode === 'disabled') return result
      const timeoutMs = waitTime(readTimeout(options), this.#flushTimeoutMs)
      const targets = [...this.#live.values()]
      this.pump()
      if (targets.length > 0 && timeoutMs > 0) await this.waitFor(targets, timeoutMs)
      for (const entry of targets) {
        if (entry.outcome) {
          result[entry.outcome]++
        } else {
          result.pending++
          // With 0 the caller chose not to wait: pending is expected, not a problem.
          if (timeoutMs > 0) {
            this.report(
              { code: 'pending_after_flush', message: 'Still pending when flush() returned.', eventId: entry.eventId },
              entry.redacted ? entry.event : undefined,
              entry.depth,
              entry.context,
            )
          }
        }
      }
    } catch {
      // flush() never rejects.
    }
    return result
  }

  /**
   * Terminal: drops events still waiting for `beforeSend`, waits up to `timeoutMs` for the rest
   * (`0`: not at all; never for a retry due after the deadline), then drops whatever is left,
   * aborting requests and retries. Later captures are dropped. By default it waits the flush
   * timeout, or 2 s when that is 0: a request path that doesn't wait still lets shutdown finish
   * what is in flight. A `timeoutMs` that can't be read (a getter that throws) counts as not given:
   * it still shuts down.
   */
  async shutdown(options?: { timeoutMs?: number | undefined }): Promise<FlushResult> {
    const result: FlushResult = { sent: 0, skipped: 0, failed: 0, pending: 0 }
    try {
      this.#closed = true
      const timeoutMs = waitTime(readTimeout(options), this.#flushTimeoutMs || DEFAULTS.flushTimeoutMs)
      const targets = [...this.#live.values()]
      for (const entry of targets) {
        if (entry.stage === 'waiting' || entry.stage === 'hook') {
          this.finish(entry, 'failed', { code: 'shutdown', message: 'shutdown() dropped an event waiting for beforeSend.' })
        }
      }
      const remaining = targets.filter((entry) => !entry.outcome)
      if (remaining.length > 0 && timeoutMs > 0) await this.waitFor(remaining, timeoutMs)
      for (const entry of [...this.#live.values()]) {
        this.finish(entry, 'failed', { code: 'shutdown', message: 'shutdown() ended before the event was sent.' })
      }
      for (const entry of targets) result[entry.outcome ?? 'failed']++
    } catch {
      // shutdown() never rejects.
    }
    return result
  }

  private enqueue(input: unknown): void {
    if (this.#mode === 'disabled') return
    // Captured from code nested past the limit (e.g. a beforeSend capturing from a hook): refused,
    // so no capture chain can grow without bound.
    if (currentDepth() > MAX_CALLBACK_DEPTH) return
    if (this.#closed) {
      // Reported once: an onError that captures after shutdown must not feed itself.
      if (!this.#reportedClosed) {
        this.#reportedClosed = true
        this.report({ code: 'shutdown', message: 'capture() was called after shutdown(); this and later events are dropped.' })
      }
      return
    }

    const normalized = normalize(input, { budget: this.#maxEventBytes })
    if (!normalized.ok) {
      const messages = {
        missing_required: 'Required fields are missing or not strings; the event was dropped.',
        invalid_conversation_id: 'conversationId must not contain "/"; the event was dropped.',
        too_large: `The event is larger than ${this.#maxEventBytes} bytes; it was dropped.`,
      }
      const error: LSquareError = { code: normalized.code, message: messages[normalized.code] }
      if (normalized.paths.length > 0) error.paths = normalized.paths
      this.report(error)
      return
    }

    const { body, dropped, ignoredFields, ignoredToolCallFields } = normalized.value
    const meta = {
      // Its own turnId: a polluted Object.prototype's would give every turn without one the same ID,
      // and the ingestion would take all but the first for duplicates.
      eventId: eventIdFor(body.conversationId, hasOwn(body, 'turnId') ? body.turnId : undefined),
      capturedAt: new Date().toISOString(),
    }
    const event = assemble(body, meta, dropped, {})
    const bytes = byteSize(event)
    if (bytes > this.#maxEventBytes) {
      this.report({ code: 'too_large', message: `The event is larger than ${this.#maxEventBytes} bytes; it was dropped.`, eventId: meta.eventId })
      return
    }
    if (!this.makeRoom(bytes)) {
      this.report({ code: 'queue_full', message: 'The queue is full; the event was dropped.', eventId: meta.eventId })
      return
    }

    const entry: Entry = {
      seq: ++this.#seq,
      depth: currentDepth(),
      context: new AsyncResource('LSquareEvent'),
      createdAt: performance.now(),
      eventId: meta.eventId,
      event,
      body: this.#beforeSend ? undefined : body,
      dropped,
      bytes,
      stage: this.#beforeSend ? 'waiting' : 'ready',
      redacted: false,
      attempts: 0,
      retryAt: undefined,
      outcome: undefined,
      zombie: false,
      hookAbort: undefined,
      fetchAbort: undefined,
      timer: undefined,
      ignoredFields,
      ignoredToolCallFields,
    }
    this.#live.set(entry.seq, entry)
    this.#liveBytes += bytes

    if (this.#beforeSend) {
      entry.timer = unref(setTimeout(() => this.expire(entry), this.#beforeSendTimeoutMs))
      queueMicrotask(() => this.pump())
    } else {
      // The closure holds only the entry, whose content is released if it's dropped before this runs.
      queueMicrotask(() => this.guard(entry, () => this.process(entry, entry.body)))
    }
  }

  /** Enforces hook deadlines and starts hooks for waiting events, within the concurrency limit. */
  private pump(): void {
    try {
      if (!this.#beforeSend) return
      for (const entry of [...this.#live.values()]) {
        if ((entry.stage === 'waiting' || entry.stage === 'hook') && this.pastDeadline(entry)) this.expire(entry)
      }
      for (const entry of [...this.#live.values()]) {
        if (this.#activeHooks >= this.#maxConcurrentHooks) break
        if (entry.stage === 'waiting') this.startHook(entry, this.#beforeSend)
      }
    } catch {
      // Never let a background step throw into the app's event loop.
    }
  }

  private startHook(entry: Entry, beforeSend: BeforeSend): void {
    let controller: AbortController
    let event: TurnEvent
    try {
      // Before taking a slot: if these fail, no slot is ever left taken.
      controller = new AbortController()
      event = copyEvent(entry.event as TurnEvent)
    } catch {
      this.finish(entry, 'failed', { code: 'internal_error', message: 'beforeSend could not be started; the event was dropped.' })
      return
    }
    entry.stage = 'hook'
    this.#activeHooks++
    entry.hookAbort = controller
    // All in the async context of its capture(), whichever event's progress started it: the hook,
    // and the callback that starts the next one. Attached anywhere else, that callback would carry
    // another event's context along every handoff, keeping it reachable while hooks stay busy.
    inContext(entry.context, () => {
      let pending: Promise<unknown>
      try {
        // The hook is a callback too: anything it captures is one level deeper than its event.
        pending = Promise.resolve(callbackDepth.run(entry.depth + 1, () => beforeSend(event, { signal: controller.signal })))
      } catch (error) {
        pending = Promise.reject(error)
      }
      pending
        .then(
          (out) => this.hookDone(entry, out, false),
          () => this.hookDone(entry, undefined, true),
        )
        .catch(() => {})
    })
  }

  private hookDone(entry: Entry, out: unknown, failed: boolean): void {
    this.#activeHooks--
    if (entry.zombie) {
      // Too late: the event was already dropped. Release what the hook held, discard its result.
      entry.zombie = false
      this.#zombies--
      this.#zombieBytes -= entry.bytes
      this.pump()
      return
    }
    if (entry.outcome) {
      this.pump()
      return
    }
    entry.stage = 'ready'
    this.clearTimer(entry)
    // The deadline timer may not have run yet if the event loop was blocked: check it here too.
    const late = this.pastDeadline(entry)
    this.guard(entry, () => {
      if (late) {
        this.finish(entry, 'failed', this.timeoutError())
      } else if (failed) {
        this.finish(entry, 'failed', { code: 'hook_failed', message: 'beforeSend threw or rejected; the event was dropped.' })
      } else if (out === null) {
        this.finish(entry, 'skipped')
      } else if (!isPlainObject(out)) {
        this.finish(entry, 'failed', { code: 'hook_failed', message: 'beforeSend must return the event or null; the event was dropped.' })
      } else {
        // The hook can change content, not identity. Its output is read field by field, never spread:
        // a spread would also run getters on fields the SDK doesn't keep.
        const original = entry.event as TurnEvent
        // Its own turnId, as in enqueue(): absent stays absent, not a polluted Object.prototype's.
        const turnId = hasOwn(original, 'turnId') ? original.turnId : undefined
        const normalized = normalize(out, {
          budget: this.#maxEventBytes,
          identity: { conversationId: original.conversationId, orgId: original.orgId, toolName: original.toolName, turnId },
        })
        if (!normalized.ok) {
          const tooLarge = normalized.code === 'too_large'
          const error: LSquareError = {
            code: tooLarge ? 'too_large' : 'hook_failed',
            message: tooLarge
              ? `beforeSend made the event larger than ${this.#maxEventBytes} bytes; it was dropped.`
              : 'beforeSend removed required fields; the event was dropped.',
          }
          if (normalized.paths.length > 0) error.paths = normalized.paths
          this.finish(entry, 'failed', error)
        } else {
          // Paths found in the hook's output refer to its positions, not the app's input.
          for (const [path, reason] of Object.entries(normalized.value.dropped)) entry.dropped[`beforeSend.${path}`] = reason
          this.process(entry, normalized.value.body)
        }
      }
    })
    this.pump()
  }

  /** Built-in redaction, size and memory checks, then dry-run log or send. */
  private process(entry: Entry, body: TurnBody | undefined): void {
    entry.body = undefined
    if (entry.outcome || body === undefined || entry.event === undefined) return
    let redacted: { value: TurnBody; counts: Partial<Record<PatternName, number>> }
    try {
      redacted = redactBody(body, this.#patterns)
    } catch {
      this.finish(entry, 'failed', { code: 'redaction_failed', message: 'Redaction failed; the event was dropped, not sent unredacted.' })
      return
    }
    const event = assemble(redacted.value, entry.event, entry.dropped, redacted.counts)
    const bytes = byteSize(event)
    this.#liveBytes += bytes - entry.bytes
    entry.bytes = bytes
    entry.event = event
    entry.redacted = true

    if (bytes > this.#maxEventBytes) {
      this.finish(entry, 'failed', { code: 'too_large', message: `The event is larger than ${this.#maxEventBytes} bytes; it was dropped.` })
    } else if (!this.fitsAfterGrowth(entry)) {
      this.finish(entry, 'failed', { code: 'queue_full', message: 'The event grew past the memory limits; it was dropped.' })
    } else if (entry.outcome) {
      // Finished while making room: never dispatch an event already reported.
    } else if (this.#mode === 'dry-run') {
      this.log('info', dryRunSummary(entry), entry.depth, entry.context)
      this.finish(entry, 'skipped')
    } else {
      this.dispatch(entry)
    }
  }

  /**
   * Sends now if a request slot is free; otherwise the event waits its turn, in the order events
   * became ready. The limit keeps a slow or unreachable LSquare from holding a socket per queued
   * event. The request runs in the event's own async context, whichever event freed the slot.
   */
  private dispatch(entry: Entry): void {
    if (this.#activeRequests >= this.#maxConcurrentRequests) {
      entry.stage = 'queued'
      this.#queued.add(entry)
      return
    }
    entry.stage = 'sending'
    this.#activeRequests++
    // send() never rejects, and every attempt ends by the request timeout, so the slot always returns.
    // The callback that starts the next event is attached in this event's context too, so no
    // handoff carries an earlier event's context along.
    inContext(entry.context, () => {
      void this.send(entry).finally(() => {
        this.#activeRequests--
        this.pumpRequests()
      })
    })
  }

  private pumpRequests(): void {
    try {
      for (const entry of this.#queued) {
        if (this.#activeRequests >= this.#maxConcurrentRequests) break
        this.#queued.delete(entry)
        this.dispatch(entry)
      }
    } catch {
      // Never let a background step throw into the app's event loop.
    }
  }

  private async send(entry: Entry): Promise<void> {
    if (entry.outcome) return
    try {
      entry.attempts++
      const controller = new AbortController()
      entry.fetchAbort = controller
      // Covers the whole exchange, including reading the response body, and settles even if a custom
      // fetch ignores the abort.
      let expire: () => void = () => {}
      const expired = new Promise<never>((_, reject) => (expire = () => reject(new Error('timeout'))))
      expired.catch(() => {})
      const timeout = unref(
        setTimeout(() => {
          controller.abort()
          expire()
        }, this.#requestTimeoutMs),
      )
      /** `body` is undefined when it was too large or unreadable. */
      let reply: { status: number; retryAfter: number | undefined; body: string | undefined } | undefined
      /** The fetch, until it has answered in time: an answer after the timeout, from a fetch that ignores the abort, has its body stopped. */
      let fetching: Promise<Response> | undefined
      /** The answer, until its body is being read or discarded: if handling it throws first, its body is stopped. */
      let unread: Response | undefined
      try {
        fetching = Promise.resolve(
          (this.#fetchImpl as typeof fetch)(this.#url, {
            method: 'POST',
            headers: { 'content-type': 'application/json', authorization: `Bearer ${this.#apiKey}` },
            body: JSON.stringify({ events: [entry.event] }),
            signal: controller.signal,
            redirect: 'error',
          }),
        )
        const response = await Promise.race([fetching, expired])
        fetching = undefined
        unread = response
        const status = response.status
        const ok = status >= 200 && status < 300
        // Only a 2xx body matters (the acknowledgement). Others are decided from status and headers.
        if (!ok) {
          unread = undefined
          discard(response.body)
        }
        const retryAfter = parseRetryAfter(response.headers.get('retry-after'))
        unread = undefined
        // The request's signal stops the read at the timeout, or when the event is dropped meanwhile.
        const body = ok ? await Promise.race([readBody(response.body, MAX_RESPONSE_BYTES, controller.signal), expired]) : ''
        reply = { status, retryAfter, body }
      } catch {
        // Network error, timeout, refused redirect, or an answer that couldn't be handled (its status
        // or headers threw). Retrying is safe: the eventId is stable.
        if (fetching) discardLate(fetching)
        if (unread) discardBodyOf(unread)
      } finally {
        clearTimeout(timeout)
        if (entry.fetchAbort === controller) entry.fetchAbort = undefined
      }
      if (entry.outcome) return
      if (!reply) {
        this.retryOrFail(entry, undefined, undefined)
        return
      }

      const { status } = reply
      if (status >= 200 && status < 300) {
        const ack = reply.body === undefined ? 'unexpected' : acknowledgement(reply.body, entry.eventId)
        if (ack === 'accepted') {
          this.finish(entry, 'sent')
        } else if (ack === 'unexpected') {
          this.finish(entry, 'failed', { code: 'send_failed', message: 'Unexpected response: LSquare did not acknowledge the event.', status })
        } else {
          this.finish(entry, 'failed', { code: 'rejected', message: `LSquare rejected the event: ${ack.rejected}.` })
        }
      } else if (status === 429 || status >= 500) {
        this.retryOrFail(entry, status, reply.retryAfter)
      } else {
        this.finish(entry, 'failed', { code: 'send_failed', message: `LSquare answered ${status}; not retried.`, status })
      }
    } catch {
      if (!entry.outcome) this.finish(entry, 'failed', { code: 'internal_error', message: 'Sending failed unexpectedly.' })
    }
  }

  private retryOrFail(entry: Entry, status: number | undefined, retryAfterMs: number | undefined): void {
    if (entry.attempts >= this.#maxAttempts) {
      const error: LSquareError = {
        code: 'send_failed',
        message:
          status === undefined
            ? `Network error after ${entry.attempts} attempts.`
            : `LSquare answered ${status} after ${entry.attempts} attempts.`,
      }
      if (status !== undefined) error.status = status
      this.finish(entry, 'failed', error)
      return
    }
    const backoff = Math.random() * Math.min(RETRY_CAP_MS, RETRY_BASE_MS * 2 ** (entry.attempts - 1))
    const delay = retryAfterMs === undefined ? backoff : Math.min(retryAfterMs, RETRY_AFTER_CAP_MS)
    entry.stage = 'retry'
    entry.retryAt = performance.now() + delay
    entry.timer = unref(
      setTimeout(() => {
        entry.timer = undefined
        entry.retryAt = undefined
        if (entry.outcome) return
        this.dispatch(entry)
      }, delay),
    )
    // A flush whose deadline comes first can't see this event finish: it stops waiting for it.
    this.release(entry)
  }

  private pastDeadline(entry: Entry): boolean {
    return performance.now() - entry.createdAt >= this.#beforeSendTimeoutMs
  }

  private timeoutError(): LSquareError {
    return { code: 'hook_timeout', message: `beforeSend did not finish within ${this.#beforeSendTimeoutMs} ms; the event was dropped.` }
  }

  private expire(entry: Entry): void {
    if (entry.outcome || (entry.stage !== 'waiting' && entry.stage !== 'hook')) return
    this.finish(entry, 'failed', this.timeoutError())
  }

  /** What eviction can't free: expired hooks still running, and events inside a running hook. */
  private pinned(except?: Entry): { count: number; bytes: number } {
    let count = this.#zombies
    let bytes = this.#zombieBytes
    for (const entry of this.#live.values()) {
      if (entry.stage === 'hook' && entry !== except) {
        count++
        bytes += entry.bytes
      }
    }
    return { count, bytes }
  }

  /** Makes room for a new event by dropping the oldest ones, unless even that couldn't make it fit. */
  private makeRoom(bytes: number): boolean {
    const pinned = this.pinned()
    if (pinned.count + 1 > this.#maxQueueEvents || pinned.bytes + bytes > this.#maxQueueBytes) return false
    return this.evictUntil(
      () =>
        this.#live.size + this.#zombies + 1 <= this.#maxQueueEvents &&
        this.#liveBytes + this.#zombieBytes + bytes <= this.#maxQueueBytes,
    )
  }

  /** After an event grew (hook output, placeholders), drops older events until the budget holds again. */
  private fitsAfterGrowth(grown: Entry): boolean {
    if (grown.bytes + this.pinned(grown).bytes > this.#maxQueueBytes) return false
    return this.evictUntil(() => this.#liveBytes + this.#zombieBytes <= this.#maxQueueBytes, grown)
  }

  /**
   * Drops the oldest events until `fits()`. Events inside a hook free nothing, so they are skipped.
   * Events with a request in flight go last: aborting them may not stop LSquare receiving them.
   */
  private evictUntil(fits: () => boolean, keep?: Entry): boolean {
    if (fits()) return true
    for (const inFlight of [false, true]) {
      for (const entry of [...this.#live.values()]) {
        if (entry === keep || entry.stage === 'hook' || (entry.stage === 'sending') !== inFlight) continue
        this.finish(entry, 'failed', {
          code: 'queue_full',
          message: inFlight
            ? 'Dropped to stay within the memory limits; its request was aborted and may still have reached LSquare.'
            : 'Dropped to stay within the memory limits.',
        })
        if (fits()) return true
      }
    }
    return false
  }

  /** Final state. Anything that happens to the event later (a late hook, a response) is ignored. */
  private finish(entry: Entry, outcome: Outcome, error?: LSquareError): void {
    if (entry.outcome) return
    entry.outcome = outcome
    this.clearTimer(entry)
    entry.fetchAbort?.abort()
    entry.fetchAbort = undefined
    if (entry.stage === 'hook') {
      entry.zombie = true
      this.#zombies++
      this.#zombieBytes += entry.bytes
      entry.hookAbort?.abort()
    }
    this.#live.delete(entry.seq)
    this.#queued.delete(entry)
    this.#liveBytes -= entry.bytes
    if (error) this.report({ eventId: entry.eventId, ...error }, entry.redacted ? entry.event : undefined, entry.depth, entry.context)
    // The report took its own copy, and its own reference to the context. Whatever still holds this
    // entry (a pending microtask, a flush, a slow fetch, a hook that ignored its signal) holds no
    // content and no request context from here.
    entry.event = undefined
    entry.body = undefined
    entry.context = undefined
    this.release(entry)
  }

  /**
   * Waits up to `timeoutMs` for `entries`. One whose next attempt is due after the deadline can't
   * finish in time, so it isn't waited for. The timer is not unref'd: an awaited flush keeps the
   * process alive until it returns.
   */
  private waitFor(entries: Entry[], timeoutMs: number): Promise<void> {
    return new Promise<void>((resolve) => {
      const deadline = performance.now() + timeoutMs
      const wait: Wait = { deadline, pending: new Set(entries.filter((entry) => !settledBy(entry, deadline))), end: resolve }
      if (wait.pending.size === 0) return resolve()
      const timer = setTimeout(() => wait.end(), timeoutMs)
      wait.end = () => {
        clearTimeout(timer)
        this.#waits.delete(wait)
        resolve()
      }
      this.#waits.add(wait)
    })
  }

  /** Lets waits stop waiting for an event that finished, or whose next attempt is after their deadline. */
  private release(entry: Entry): void {
    for (const wait of this.#waits) {
      if (!wait.pending.has(entry) || !settledBy(entry, wait.deadline)) continue
      wait.pending.delete(entry)
      if (wait.pending.size === 0) wait.end()
    }
  }

  private guard(entry: Entry, step: () => void): void {
    try {
      step()
    } catch {
      this.finish(entry, 'failed', { code: 'internal_error', message: 'Processing failed unexpectedly; the event was dropped.' })
    }
  }

  private clearTimer(entry: Entry): void {
    if (entry.timer !== undefined) clearTimeout(entry.timer)
    entry.timer = undefined
  }

  /**
   * `event` is passed only once built-in redaction has run on it, and only as a copy: changing it
   * can't change what is sent. The handler runs in a later event-loop turn, so it can't re-enter
   * the SDK in the middle of a step (capturing from `onError` is safe).
   */
  /**
   * `origin` is the depth of the event the report is about. Only it counts, not whatever code
   * happens to be running: a hook freed by deep callback code must not make another event's
   * reports deep too. Reports not about a queued event use the current depth.
   */
  private report(error: LSquareError, event?: TurnEvent, origin = currentDepth(), context?: AsyncResource): void {
    try {
      const depth = origin + 1
      if (depth > MAX_CALLBACK_DEPTH || !this.#reportLimit.take()) return
      const suppressed = this.#reportLimit.drain()
      const delivered = suppressed > 0 ? { ...error, suppressed } : error
      // Only a custom handler sees the event, so only then is it copied.
      const snapshot = this.#onErrorHandler && event !== undefined ? structuredClone(event) : undefined
      setImmediate(() => inContext(context, () => this.deliver(delivered, snapshot, depth)))
    } catch {
      // onError must never break the app.
    }
  }

  private deliver(error: LSquareError, event: TurnEvent | undefined, depth: number): void {
    try {
      const handler = this.#onErrorHandler
      if (handler) {
        callbackDepth.run(depth, () => swallow(handler(error, event)))
        return
      }
      // Reports the rate limit dropped before they got here count as suppressed too.
      this.#suppressedErrors += error.suppressed ?? 0
      const now = performance.now()
      if (now - this.#lastErrorLog < ERROR_LOG_INTERVAL_MS) {
        this.#suppressedErrors++
        return
      }
      const more = this.#suppressedErrors > 0 ? ` (${this.#suppressedErrors} more since the last report)` : ''
      this.#lastErrorLog = now
      this.#suppressedErrors = 0
      this.writeLog('warn', `[lsquare] ${error.code}: ${error.message}${more}`, depth)
    } catch {
      // onError must never break the app.
    }
  }

  /** Logs in a later turn, within the depth and rate limits, so a logger can't re-enter the SDK mid-step. */
  private log(level: 'info' | 'warn', message: string, origin = currentDepth(), context?: AsyncResource): void {
    try {
      const depth = origin + 1
      if (depth > MAX_CALLBACK_DEPTH || !this.#logLimit.take()) return
      const suppressed = this.#logLimit.drain()
      const line = suppressed > 0 ? `${message} [lsquare: ${suppressed} earlier log lines suppressed]` : message
      setImmediate(() => inContext(context, () => this.writeLog(level, line, depth)))
    } catch {
      // Ignore.
    }
  }

  /** Logging must never break the app, even with a logger that throws or returns a rejected promise. */
  private writeLog(level: 'info' | 'warn', message: string, depth: number): void {
    try {
      // The default logger can't call back into the SDK, so it needs no depth (see callbackDepth).
      if (this.#customLogger) callbackDepth.run(depth, () => swallow(this.#logger[level](message)))
      else swallow(this.#logger[level](message))
    } catch {
      // Ignore.
    }
  }
}

/** Catches the rejection of whatever thenable a callback returned. */
function swallow(value: unknown): void {
  try {
    if ((typeof value === 'object' || typeof value === 'function') && value !== null) {
      if (typeof (value as { then?: unknown }).then === 'function') Promise.resolve(value).catch(() => {})
    }
  } catch {
    // A throwing `then` getter: nothing to wait for.
  }
}

/** Finished, or not due to try again before `deadline`. */
function settledBy(entry: Entry, deadline: number): boolean {
  return entry.outcome !== undefined || (entry.retryAt !== undefined && entry.retryAt >= deadline)
}

function inContext<T>(context: AsyncResource | undefined, run: () => T): T {
  return context ? context.runInAsyncScope(run) : run()
}

/**
 * A deep copy of the SDK's own event, for `beforeSend`, made of this realm's objects and arrays. Not
 * the global structuredClone, which a test runner's vm context (Jest's) may take from another realm:
 * its objects would fail the checks on the hook's output. Not a JSON round trip either: that would
 * run a `toJSON` the event inherits (a polluted Object.prototype's) and hand the hook what it made.
 * The event holds only data the SDK built, so its own enumerable properties are all there is.
 * Object.fromEntries and `map` create each property as data, never by assignment, so no inherited
 * setter runs; `-0` stays `-0`. One call per object or array, not one per key: an
 * `Object.defineProperty` per key, each with a new descriptor, was slower than structuredClone.
 */
function copyEvent<T>(value: T): T {
  if (typeof value !== 'object' || value === null) return value
  // The SDK's own arrays: their `map` makes this realm's arrays.
  if (Array.isArray(value)) return value.map((item: unknown) => copyEvent(item)) as T
  const source = value as Record<string, unknown>
  return Object.fromEntries(Object.keys(source).map((key) => [key, copyEvent(source[key])])) as T
}

function assemble(
  body: TurnBody,
  meta: { eventId: string; capturedAt: string },
  dropped: Record<string, DroppedReason>,
  redactions: Partial<Record<PatternName, number>>,
): TurnEvent {
  const event: TurnEvent = {
    eventId: meta.eventId,
    capturedAt: meta.capturedAt,
    source: 'live',
    schemaVersion: 1,
    sdk: { name: SDK_NAME, version: SDK_VERSION },
    ...body,
  }
  if (Object.keys(redactions).length > 0) event.redactions = redactions
  if (Object.keys(dropped).length > 0) event.dropped = { ...dropped }
  return event
}

/** One line per event: shape only, never content. */
function dryRunSummary(entry: Entry): string {
  // Called while the entry is live, before it is finished and released.
  const event = entry.event as TurnEvent
  const summary: Record<string, unknown> = {
    // Where the ID came from, never the ID: it contains the conversation and turn IDs. '/t/' first: a
    // hashed ID (`h/` and hex) never has it, but an unhashed one starts with `h/` too when the
    // conversation ID is 'h'.
    eventId: event.eventId.includes('/t/') ? 'from turnId' : event.eventId.startsWith('h/') ? 'from turnId, hashed' : 'random: no turnId',
    fields: Object.keys(event).filter((key) => !SDK_FIELDS.has(key)).sort(),
    bytes: entry.bytes,
  }
  // The event's own, as `fields` is: never what a polluted Object.prototype gives it.
  if (hasOwn(event, 'toolCalls') && event.toolCalls) summary.toolCalls = event.toolCalls.length
  if (hasOwn(event, 'dropped')) summary.dropped = event.dropped
  if (hasOwn(event, 'redactions')) summary.redactions = event.redactions
  if (Object.keys(entry.ignoredFields).length > 0) summary.ignoredFields = entry.ignoredFields
  if (Object.keys(entry.ignoredToolCallFields).length > 0) summary.ignoredToolCallFields = entry.ignoredToolCallFields
  return `[lsquare dry-run] ${JSON.stringify(summary)}`
}

/**
 * The event counts as sent only if LSquare names it as accepted or duplicate, and not as rejected.
 * LSquare rejects an event whose `eventId` it can't use (one with a NUL or a lone surrogate, from
 * the conversation or turn ID) by its position instead, without an `eventId`: each request carries
 * one event, so such a rejection at index 0 is this event's.
 */
function acknowledgement(text: string, eventId: string): 'accepted' | 'unexpected' | { rejected: string } {
  let body: unknown
  try {
    body = JSON.parse(text)
  } catch {
    return 'unexpected'
  }
  if (!isPlainObject(body)) return 'unexpected'
  const listed = (key: string) => Array.isArray(body[key]) && (body[key] as unknown[]).includes(eventId)
  const rejected = Array.isArray(body.rejected)
    ? (body.rejected as unknown[]).find(
        (item) => isPlainObject(item) && (item.eventId === eventId || (!hasOwn(item, 'eventId') && hasOwn(item, 'index') && item.index === 0)),
      )
    : undefined
  const accepted = listed('accepted') || listed('duplicates')
  if (rejected !== undefined && accepted) return 'unexpected'
  if (rejected !== undefined) {
    const reason = (rejected as Record<string, unknown>).reason
    return { rejected: typeof reason === 'string' && REJECTION_REASONS.has(reason) ? reason : 'unspecified' }
  }
  return accepted ? 'accepted' : 'unexpected'
}

/**
 * Reads at most `limit` bytes; undefined when larger or unreadable. A web stream is read through its
 * reader (not every implementation is async-iterable); a Node stream, as some fetch polyfills
 * return, by async iteration. Stopping early cancels or destroys either, and so does `signal`, the
 * request's, aborted at its timeout or when its event is dropped: with a custom fetch that ignores
 * the abort, the read doesn't stay stalled behind. Best effort: a stream that ignores its cancel or
 * destroy is left to itself.
 */
async function readBody(body: unknown, limit: number, signal: AbortSignal): Promise<string | undefined> {
  if (body === null || body === undefined) return ''
  const chunks: Uint8Array[] = []
  let total = 0
  const keep = (chunk: unknown): boolean => {
    // A string's UTF-8 size is at least its length: refuse an oversized one before copying it.
    if (typeof chunk === 'string' && total + chunk.length > limit) return false
    const bytes = typeof chunk === 'string' ? Buffer.from(chunk) : chunk
    // From any realm: in a test runner's vm context, the runtime's fetch yields the outer realm's.
    if (!types.isUint8Array(bytes)) return false
    total += bytes.byteLength
    chunks.push(bytes)
    return total <= limit
  }
  const stream = body as { getReader?: unknown; [Symbol.asyncIterator]?: unknown }
  quietErrors(body)
  if (typeof stream.getReader === 'function') {
    const reader = (stream.getReader as () => ReadableStreamDefaultReader<Uint8Array | string>).call(body)
    const stop = () => {
      try {
        swallow(reader.cancel())
      } catch {
        // Nothing more to do.
      }
    }
    const off = whenAborted(signal, stop)
    try {
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        if (!keep(value)) {
          stop()
          return undefined
        }
      }
    } finally {
      off()
    }
  } else if (typeof stream[Symbol.asyncIterator] === 'function') {
    const off = whenAborted(signal, () => discard(body))
    try {
      for await (const chunk of body as AsyncIterable<Uint8Array | string>) if (!keep(chunk)) return undefined
    } finally {
      off()
    }
  } else {
    return undefined
  }
  return Buffer.concat(chunks).toString('utf8')
}

/** Stops a body the SDK won't read: a web stream is cancelled, a Node stream destroyed. */
function discard(body: unknown): void {
  try {
    const stream = body as { cancel?: unknown; destroy?: unknown } | null | undefined
    if (typeof stream?.cancel === 'function') {
      swallow((stream.cancel as () => unknown).call(stream))
    } else if (typeof stream?.destroy === 'function') {
      quietErrors(stream)
      ;(stream.destroy as () => unknown).call(stream)
    }
  } catch {
    // Nothing more to do.
  }
}

/** Runs `stop` once `signal` is aborted, at once if it already is. Returns what stops listening. */
function whenAborted(signal: AbortSignal, stop: () => void): () => void {
  try {
    if (signal.aborted) {
      stop()
      return () => {}
    }
    signal.addEventListener('abort', stop, { once: true })
    return () => signal.removeEventListener('abort', stop)
  } catch {
    // Unwatched, the body is still read, as before, and the race with the timeout still settles.
    return () => {}
  }
}

/** Stops the body of a response that comes too late to be read, if it ever comes. */
function discardLate(fetching: Promise<Response>): void {
  fetching.then(discardBodyOf, () => {})
}

/** Stops the body of a response the SDK won't read, even one whose `body` getter throws. */
function discardBodyOf(response: unknown): void {
  try {
    discard((response as { body?: unknown } | null | undefined)?.body)
  } catch {
    // Nothing more to do.
  }
}

/**
 * Gives a Node stream an 'error' listener. One can emit 'error' after the SDK is done with it (while
 * being destroyed, say); with nobody listening, Node would throw it into the app's process.
 */
function quietErrors(stream: unknown): void {
  try {
    const emitter = stream as { on?: unknown } | null | undefined
    if (typeof emitter?.on === 'function') (emitter.on as (event: string, listener: () => void) => unknown).call(stream, 'error', () => {})
  } catch {
    // Not an event emitter after all.
  }
}

function resolveEndpoint(raw: string): { url: string } | { problem: string } {
  let url: URL
  try {
    url = new URL(raw)
  } catch {
    return { problem: 'The endpoint is not a valid URL' }
  }
  const secure = url.protocol === 'https:' || (url.protocol === 'http:' && LOCAL_HOSTS.has(url.hostname))
  if (!secure) return { problem: 'The endpoint must use https (http only for localhost)' }
  return { url: `${url.origin}${url.pathname.replace(/\/+$/, '')}/v1/turns` }
}

function parseRetryAfter(header: string | null): number | undefined {
  if (!header) return undefined
  if (/^\d+$/.test(header.trim())) return Number(header.trim()) * 1000
  const date = Date.parse(header)
  return Number.isNaN(date) ? undefined : Math.max(0, date - Date.now())
}

function byteSize(value: unknown): number {
  return Buffer.byteLength(JSON.stringify(value), 'utf8')
}

function positive(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : fallback
}

/** Node's longest timer (about 24.8 days). Longer values make Node warn and fire after 1 ms. */
const MAX_TIMER_MS = 2_147_483_647

function duration(value: unknown, fallback: number): number {
  return Math.min(positive(value, fallback), MAX_TIMER_MS)
}

/**
 * flush()'s and shutdown()'s `timeoutMs`, read once without throwing: one that can't be read (a
 * getter or a Proxy trap that throws) counts as not given, as one that isn't a number does.
 */
function readTimeout(options: unknown): unknown {
  try {
    return readSettings(options, ['timeoutMs']).timeoutMs
  } catch {
    return undefined
  }
}

/** Like `duration`, but 0 is allowed: don't wait. */
function waitTime(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? Math.min(value, MAX_TIMER_MS) : fallback
}

/** At least 1: a fraction such as 0.5 must not floor to a limit of 0, which would block everything. */
function positiveInt(value: unknown, fallback: number): number {
  return Math.max(1, Math.floor(positive(value, fallback)))
}

/** Set variables only: for an unset name, process.env falls back to Object.prototype, polluted or not. */
function env(name: string): string | undefined {
  const vars = typeof process !== 'undefined' ? process.env : undefined
  return vars && hasOwn(vars, name) ? vars[name] : undefined
}

function unref<T>(timer: T): T {
  ;(timer as { unref?: () => void }).unref?.()
  return timer
}
