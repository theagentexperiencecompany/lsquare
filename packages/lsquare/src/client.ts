import { AsyncLocalStorage, AsyncResource } from 'node:async_hooks'
import { types } from 'node:util'
import { afterHook, buildCall, buildTurn, type Built, byteSize, type CallRecord, capturedIds, type Context, type Draft, readAgent, retainedBytes } from './event.js'
import { adopted, blank, definedBelowObjectPrototype, follow, hasOwn, isPlainObject, put, readFields, readSettings, silence } from './json.js'
import { idValue, SDK_FIELDS, turnIdValue, turnKey, valid } from './normalize.js'
import { looksLikeContact, readKeys, resolvePatterns } from './redact.js'
import { defaultLogger, isConsole, isWorkers, runtimeFetch, runtimeName } from './runtime.js'
import type {
  BeforeSend,
  CaptureInput,
  FlushResult,
  Logger,
  LSquareError,
  LSquareEvent,
  LSquareOptions,
  PatternName,
} from './types.js'

export const SDK_NAME = 'lsquare'
export const SDK_VERSION = '0.2.0'

/** How the wrapper (`lsquare/mcp`) reaches a client: by this key, never `instanceof`, so two copies of the SDK still meet. */
export const INTERNAL = Symbol.for('lsquare.client.internal/1')

/**
 * A `tools/call` in progress on an instrumented server, as `capture()` and `flush()` inside it see
 * it. Lives in the client's own AsyncLocalStorage: one client's calls never mix with another's.
 * Once the call's event is built or dropped, the wrapper lets go of what it set here (`agent`,
 * `thrown`, `toolResult`, `onToolEntry`), for good (`released`): a context that still holds the
 * scope, as a turn captured inside the call does until it is sent, keeps only the call's ID, its
 * turns and its boundary.
 */
export interface CallScope {
  callId: string
  /** The tool has returned: a capture() from now on is a turn of its own. */
  returned: boolean
  /** The first capture() before the tool returned: its IDs, its agent part read then (`readAgent`). */
  agent: Record<string, unknown> | undefined
  /** flush() was called inside the call: its event is sent before the reply. */
  flushRequested: boolean
  /**
   * The turns captured inside the call before the tool returned and not yet sent or dropped, at
   * most MAX_CALL_TURNS at once: the call's sending waits for them with its event. The client's
   * own, filled and emptied by it alone.
   */
  turns: Set<object>
  /** The tool's own function ran (high-level servers): an isError result without it is the SDK's. */
  invoked: boolean
  /** What the tool's own function threw, which the SDK turned into an isError result. */
  thrown: { error: unknown } | undefined
  /** What the tool's own function returned, to tell its isError results from the SDK's. */
  toolResult: { value: unknown } | undefined
  /** Run once when the tool's own function has been entered: `identify` starts then. */
  onToolEntry: (() => void) | undefined
  /**
   * The wrapper has let go of the above: a run of the tool's own function ending later (from work
   * the tool left running) records nothing here.
   */
  released: boolean
}

/** What the wrapper (`lsquare/mcp`) uses. It keeps no state of its own: what it needs lives here. */
export interface Internal {
  mode: 'disabled' | 'dry-run' | 'send'
  /**
   * Queues a call's event. `diagnostic`, what the call reports of its `identify`, is reported as
   * the event is admitted, unless it is refused for `shutdown()`, which is reported instead: never
   * a claim that a call dropped for it was sent.
   */
  record(call: CallRecord, diagnostic?: LSquareError): void
  /** The call tracking, started by the first instrument(). */
  calls(): AsyncLocalStorage<CallScope>
  flushTimeoutMs: number
  maxConcurrentHooks: number
  /** `identify` hooks running now, for the hook limit. */
  identifying: { active: number }
  /** The ID fields the last successful `identify` returned. */
  identified: { keys: readonly string[] | undefined }
  /**
   * Settles once the event with this ID, and each of a call's `turns` (see CallScope) still
   * queued, is sent or dropped, or after `timeoutMs`: a retry due later is waited for.
   */
  settled(eventId: string | undefined, timeoutMs: number, turns?: Iterable<object>): Promise<void>
  /**
   * A call whose event is still being built: flush() and shutdown() wait for it and count it. Called
   * in the call's request: a flush reports it there while it is unbuilt. Never rejects.
   */
  track(callId: string, finishing: Promise<void>): void
  /**
   * Settles once the reports and log lines raised so far have reached their handlers: at once when
   * none is still to come; on Node, once it has run them itself, or at once from a callback; on
   * Workers, one event-loop turn later. Never rejects.
   */
  delivered(): Promise<void>
  flush(timeoutMs: number): Promise<void>
  /** Runs the app's callback (`identify`) as a callback: one level deeper, outside any call. */
  callback<T>(run: () => T): T
  log(level: 'info' | 'warn', message: string): void
  /** Logs once per client. */
  warnOnce(key: string, message: string): void
  report(error: LSquareError): void
  /**
   * The client's Clock, for the wrapper's times too: a call's duration, its reply, send and
   * identify deadlines, and a tool list read's wait, give-up and pause. It holds nothing else, so
   * what keeps it (a read still running) keeps no client.
   */
  clock: { now(): number }
  validId(value: unknown): boolean
  /** A turnId as events keep it: a non-empty string or a safe integer, as a string, within the size limit. */
  turnId(value: unknown): string | undefined
  turnKey(orgId: string | undefined, conversationId: string, turnId: string): string
  /**
   * `shutdown()` has run: identify isn't called from here, and every event is dropped, which is
   * reported once. Optional: a client from another copy of lsquare may not have it, and its
   * `record()` then takes no diagnostic.
   */
  closed?(): boolean
}

/** What capture() reads of its input inside a call, once each. */
const CAPTURE_FIELDS = [
  'conversationId', 'orgId', 'userId', 'toolName', 'question', 'turnId', 'answer', 'status', 'error', 'toolCalls', 'usage',
  'latency', 'verification', 'client', 'apiKeyId', 'surface', 'metadata', 'callId',
]

const DEFAULTS = {
  flushTimeoutMs: 2_000,
  maxConcurrentHooks: 16,
  maxConcurrentRequests: 4,
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
  redactKeys: true,
  onError: true,
  logger: true,
  maxQueueEvents: true,
  maxQueueBytes: true,
  maxEventBytes: true,
  maxAttempts: true,
  requestTimeoutMs: true,
  fetch: true,
}
/** Tells a body's chunk by its internal slot: `instanceof` would ask a Proxy for its prototype. */
const { isUint8Array } = types
/** The ingestion's limits for one request. */
const MAX_BATCH_EVENTS = 100
const MAX_BATCH_BYTES = 1024 * 1024
/** The ingestion answers 413 to a batch whose acknowledgement could pass this. */
const MAX_ACK_BYTES = 60 * 1024
const ACK_OVERHEAD = '{"accepted":[],"duplicates":[],"rejected":[]}'.length
/** As long as any `resetsAt`: `toISOString` always writes 24 characters. */
const LONGEST_RESETS_AT = '2000-01-01T00:00:00.000Z'
const BATCH_OVERHEAD = '{"events":[]}'.length
/** While a daily limit holds, one batch is sent this often to check whether it was raised. */
const QUOTA_PROBE_MS = 15 * 60_000
const RETRY_BASE_MS = 250
/**
 * Cloudflare Workers runs nothing of one request from another: entering another request's async
 * context throws, a promise it settles may continue in the wrong request (depending on a
 * compatibility flag), and a request that ends takes its fetches and timers with it. So there, a
 * request waiting for events (flush, shutdown, the wrapper's waitUntil) checks on them this often:
 * it starts the hooks and requests of its own events, takes back what an ended request left
 * (reclaim()), and ends its own wait.
 */
const WATCH_MS = 50
/**
 * Cloudflare Workers: a running hook's timer beats this often, in the hook's own request. A
 * request that ends takes its timers with it, so a hook whose beat has stopped for HOOK_GONE_MS
 * went with its request and will never settle: its slot and bytes come back. It beats until
 * HOOK_BEAT_FOR_MS past the hook's deadline, no longer: a timer kept would keep a Durable Object
 * alive (and unable to hibernate), and the SDK can't tell one from a plain Worker. A hook still
 * running then gives up its slot all the same. A report's or log line's later turn that hasn't
 * started HOOK_GONE_MS after it was due went with its request too: its slot comes back (see
 * RateLimit.reclaim). So does the slot of an async `onError` or logger that started, whose beat,
 * from its start, has stopped (see RateLimit.hold).
 */
const HOOK_BEAT_MS = 1_000
const HOOK_GONE_MS = 5_000
const HOOK_BEAT_FOR_MS = 60_000
/**
 * Cloudflare Workers: how many events a pump that no waiting request runs tries to enter before
 * leaving the rest to their own requests' watch (each one that can't be entered costs a throw).
 */
const ENTER_TRIES = 4
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
 * How many of a call's turns (see CallScope.turns) its sending waits for at once: a turn captured
 * while that many are still being sent goes as usual, only not waited for. A long-running tool
 * capturing many turns while the ingestion is slow holds no more than this.
 */
const MAX_CALL_TURNS = 100

/** The email-like `userId` warning is given once per process (per copy of the SDK). */
let warnedContactUserId = false

/**
 * Backstop for loops the depth can't see (work handed to a queue or promise set up outside the
 * callback): callbacks run in a later event-loop turn, so no loop can starve timers or I/O (one
 * through an awaited flush(), which runs those queued from a microtask, for at most a burst: see
 * LSquare.delivered()), and at most this many per second per client, so any loop dies out. It
 * bounds how many are held at once too (see RateLimit).
 */
const CALLBACKS_PER_SECOND = 100

/** A report's or log line's place in its RateLimit, from admission until its handler returns, or settles if it's async. */
interface Slot {
  /** How many were dropped before it was admitted: it carries the count. */
  readonly suppressed: number
  /** The bytes of the event copy it holds; undefined when it holds none. */
  readonly bytes: number | undefined
  released: boolean
  /** Its later turn is queued, and hasn't started or been taken back. */
  queued: boolean
  /** Workers: its running async handler's last beat, on the client's Clock, and the timer beating (see RateLimit.hold). */
  beat: number
  beats: ReturnType<typeof setInterval> | undefined
}

/**
 * A client's clock: performance.now(), except that it never goes back. For every time the client
 * keeps and compares later, or takes the time between: a rate's refill, when a later turn was due
 * and a running handler's last beat (see RateLimit); an event's capture (its hook's deadline), its
 * hook's last beat and when that beat stops (see LSquare.reclaimHooks()); a request's deadline, a
 * retry's time and a wait's deadline (see LSquare.reclaim()), and how long a flush waited for
 * calls; when a daily limit is next checked; and the wrapper's times (see Internal.clock). Not
 * when that limit resets, a date (Date.now()), nor the default log's interval, which takes a clock
 * gone back for long ago (LSquare.deliver()).
 * Once performance.now() has gone back (an app's fake timers fake it from 0), it goes on from its
 * last reading, so what was due, beating or to stop that long before that reading is as long before
 * now: neither held until the old clock's time comes round again nor taken for gone at once. The
 * time between that reading and the clock going back counts as none. One per client, read by all of
 * these alike, whichever reads it first after the jump; it holds nothing else, so what holds a
 * limit (an async handler still running) doesn't hold the client.
 */
class Clock {
  /** How far performance.now() has gone back, all told. */
  private behind = 0
  /** The latest performance.now() it read. */
  private seen = performanceNow(0)

  now(): number {
    const now = performanceNow(this.seen)
    if (now < this.seen) this.behind += this.seen - now
    this.seen = now
    return now + this.behind
  }
}

/**
 * performance.now(), or `fallback` where it can't be read: a `performance` or `now` getter that
 * throws, or one that gives what isn't a function or a number (a rejected promise, silenced, as is
 * what `now()` gives). Its `now` is read once, checked, and the one checked called, through
 * Reflect.apply. A client's clock is made as the client is, in a field initializer, outside the
 * constructor's guard: whatever the runtime's `performance`, the client is still made, and its
 * clock stands still.
 */
function performanceNow(fallback: number): number {
  try {
    const clock: unknown = performance
    silence(clock)
    if ((typeof clock !== 'object' && typeof clock !== 'function') || clock === null) return fallback
    const method: unknown = (clock as { now?: unknown }).now
    silence(method)
    if (typeof method !== 'function') return fallback
    const now: unknown = Reflect.apply(method, clock, [])
    silence(now)
    return typeof now === 'number' && !Number.isNaN(now) ? now : fallback
  } catch {
    return fallback
  }
}

/**
 * The rate, and at most CALLBACKS_PER_SECOND held at once, from admission until the handler
 * returns (an async one until it settles), with event copies of at most `maxBytes` between them:
 * handlers that never settle can't hold more. One past the rate or the slots is dropped and
 * counted; one past the bytes goes without its copy.
 */
class RateLimit {
  private tokens = CALLBACKS_PER_SECOND
  /** The times it keeps (`last`, `waiting`, a slot's beat) are on this, which never goes back. */
  private readonly clock: Clock
  private last: number
  private held = 0
  private heldBytes = 0
  /** Slots whose later turn is queued, and hasn't started or been taken back (see LSquare.delivered()). */
  private queued = 0
  /** Workers: slots whose later turn hasn't started, each with when it was due, in that order. */
  private readonly waiting = new Map<Slot, number>()
  /** Workers: slots whose async handler is running, each beating in the handler's own request. */
  private readonly running = new Set<Slot>()
  /** Node: slots whose later turn is queued, with that turn, in order; shared by a client's limits (see LSquare.delivered()). */
  private readonly turns: Map<Slot, () => void>
  suppressed = 0

  constructor(turns: Map<Slot, () => void>, clock: Clock) {
    this.turns = turns
    this.clock = clock
    this.last = clock.now()
  }

  /**
   * Admits one, with an event copy of `bytes` (undefined: it has none), handing it the count of
   * those dropped before it; undefined when it's dropped, and counted. Past `maxBytes` held it holds
   * no copy, though one alone always fits.
   */
  take(bytes?: number, maxBytes = 0): Slot | undefined {
    const now = this.clock.now()
    if (this.waiting.size > 0 || this.running.size > 0) this.reclaim(now)
    // A clock that went back (an app's fake timers fake performance.now() from 0) refills nothing,
    // rather than owing tokens, and dropping everything, for as long as it went back (see Clock).
    this.tokens = Math.min(CALLBACKS_PER_SECOND, this.tokens + ((now - this.last) / 1000) * CALLBACKS_PER_SECOND)
    this.last = now
    if (this.tokens < 1 || this.held >= CALLBACKS_PER_SECOND) {
      this.suppressed++
      return undefined
    }
    const copied = bytes !== undefined && (this.heldBytes === 0 || this.heldBytes + bytes <= maxBytes) ? bytes : undefined
    this.tokens -= 1
    this.held++
    this.heldBytes += copied ?? 0
    const slot: Slot = { suppressed: this.suppressed, bytes: copied, released: false, queued: false, beat: 0, beats: undefined }
    this.suppressed = 0
    return slot
  }

  /** Gives the slot back, once. `refused`: it was never delivered, so the count it carried goes to the next one. */
  release(slot: Slot, refused = false): void {
    if (slot.released) return
    slot.released = true
    this.held--
    this.heldBytes -= slot.bytes ?? 0
    if (refused) this.suppressed += slot.suppressed
    if (slot.beats !== undefined) {
      try {
        // From another request (reclaim()), a no-op: that timer went with its own.
        clearInterval(slot.beats)
      } catch {
        // Ignore.
      }
      slot.beats = undefined
    }
    this.running.delete(slot)
  }

  /**
   * `slot`'s handler is due in a later turn, `turn`; on Workers (`isolated`), one that goes with its
   * request if that ends first. Elsewhere delivered() may run `turn` before its own does.
   */
  wait(slot: Slot, isolated: boolean, turn: () => void): void {
    slot.queued = true
    this.queued++
    if (isolated) this.waiting.set(slot, this.clock.now())
    else this.turns.set(slot, turn)
  }

  /**
   * Its later turn has started, the first time only. False when it started already (delivered()
   * ran it) or its slot was taken back meanwhile: the turn does nothing.
   */
  start(slot: Slot): boolean {
    const queued = slot.queued
    this.unqueue(slot)
    return queued && !slot.released
  }

  /** How many later turns are still to come, those that went with their request (Workers) taken back first. */
  pending(): number {
    if (this.waiting.size > 0) this.reclaim(this.clock.now())
    return this.queued
  }

  /**
   * Workers: `slot`'s async handler started, in its own request, and holds the slot until it
   * settles. A request that ends takes the handler's timers and I/O with it, so it may never
   * settle: a timer beats in that request (see HOOK_BEAT_MS) while the handler runs, at most
   * HOOK_BEAT_FOR_MS, and once the beat has been still for HOOK_GONE_MS the slot comes back (see
   * reclaim()). A soft bound: a beat that stopped doesn't prove the request ended.
   */
  hold(slot: Slot): void {
    if (slot.released) return
    const clock = this.clock
    slot.beat = clock.now()
    const stops = slot.beat + HOOK_BEAT_FOR_MS
    slot.beats = setInterval(() => {
      slot.beat = clock.now()
      if (slot.beat < stops) return
      clearInterval(slot.beats)
      slot.beats = undefined
    }, HOOK_BEAT_MS)
    this.running.add(slot)
  }

  private unqueue(slot: Slot): void {
    if (!slot.queued) return
    slot.queued = false
    this.queued--
    this.waiting.delete(slot)
    this.turns.delete(slot)
  }

  /**
   * Workers: a later turn that hasn't started HOOK_GONE_MS after it was due went with its request
   * (see HOOK_BEAT_MS), and never runs: its slot comes back, refused, so the count it carried goes
   * to the next one. The clock moves only at I/O there, so a turn still due in a request that lives
   * isn't taken for gone. A handler that started keeps its slot until it returns or settles, or,
   * an async one, until its beat has been still for HOOK_GONE_MS (see hold()): delivered, so the
   * count it carried went with it.
   */
  private reclaim(now: number): void {
    for (const [slot, due] of this.waiting) {
      if (now - due < HOOK_GONE_MS) break
      this.unqueue(slot)
      this.release(slot, true)
    }
    for (const slot of this.running) {
      if (now - slot.beat >= HOOK_GONE_MS) this.release(slot)
    }
  }
}
const MAX_RESPONSE_BYTES = 64 * 1024
const LOCAL_HOSTS = new Set(['localhost', '127.0.0.1', '[::1]'])
/** Where events go unless the `endpoint` option or LSQUARE_ENDPOINT says otherwise. */
export const DEFAULT_ENDPOINT = 'https://ingest.lsquare.io'
/** Reasons the ingestion service may give. Anything else is reported as "unspecified". */
const REJECTION_REASONS = new Set([
  'missing_field', 'invalid_field', 'invalid_conversation_id', 'too_large', 'unsupported_schema_version', 'quota_exceeded',
])

type Outcome = 'sent' | 'skipped' | 'failed'
/** `queued`: ready to send, waiting for a request slot. */
type Stage = 'waiting' | 'hook' | 'ready' | 'queued' | 'sending' | 'retry'

/** One request: the events it carries share its abort, which fires only once none of them is still live. */
interface Batch {
  live: number
  controller: AbortController
  /** Sent while a daily limit holds, to check whether it still does. */
  probe: boolean
  /** The hold-back it checks: its answer ends that one only, never a later one. */
  holdback: Holdback | undefined
  entries: Entry[]
  /** Its first event's, which it runs in: an abort of its signal is delivered there. */
  context: AsyncResource | undefined
  /** The client's Clock by which its own timeout has ended it, wherever that timer still runs. */
  deadline: number
  /** Its slot is back: once, by whichever comes first, its own end or reclaim(). */
  ended: boolean
}

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
  /** The client's Clock at capture, which never goes back: for its hook's deadline. */
  createdAt: number
  eventId: string
  kind: 'turn' | 'tool_call'
  /** Released (undefined) once the entry is finished, so nothing still holding the entry holds content. */
  event: LSquareEvent | undefined
  /** Until `beforeSend` returns: what finishing the event needs. Released like `event`. */
  draft: Draft | undefined
  bytes: number
  stage: Stage
  /** Built-in redaction has run on `event`. */
  redacted: boolean
  attempts: number
  /** The client's Clock at the next attempt while waiting to retry. */
  retryAt: number | undefined
  outcome: Outcome | undefined
  /** Finished while its hook was still running: keeps its slot and bytes until the hook settles. */
  zombie: boolean
  /** Waiting for this event to be finished: the wrapper's waitUntil and flushBeforeReply. */
  waiters: Array<() => void> | undefined
  hookAbort: AbortController | undefined
  /**
   * Finished while its hook ran: its timer, in the hook's own context, aborts the hook's signal, so
   * the app's listeners run there, unless the hook settles first. That is a timer set by finish(),
   * or, where the hook's context can't be entered from there, its deadline timer.
   */
  abortLater: boolean
  /** Its hook's slot was taken back (Workers: see HOOK_BEAT_MS): a hook settling late releases nothing. */
  slotGone: boolean
  /** Workers: its running hook's last beat, on the client's Clock, and the timer beating. */
  beat: number
  beats: ReturnType<typeof setInterval> | undefined
  batch: Batch | undefined
  timer: ReturnType<typeof setTimeout> | undefined
  /** The scheduled retry's own: its timer and reclaim() each act only on the retry still current. */
  retryToken: number | undefined
  ignoredFields: Record<string, number>
  ignoredToolCallFields: Record<string, number>
}

/** A `flush()` or `shutdown()` waiting for events, until each is finished or can't be by `deadline`. */
interface Wait {
  /** On the client's Clock. */
  deadline: number
  pending: Set<Entry>
  end: () => void
  /** Cloudflare Workers: its request's, where alone it may be ended (see WATCH_MS). */
  context: AsyncResource | undefined
}

/**
 * Cloudflare Workers: a waiting request's pumps try to start only the events it waits for, and
 * never again one it found it can't enter: a context that can't be entered from a request never can be.
 */
interface Scope {
  wanted: (entry: Entry) => boolean
  refused: WeakSet<Entry>
}

/** A call that has replied, whose event the wrapper is still building. */
interface FinishingCall {
  /** Its event's ID, which a flush reports it by while it is unbuilt. */
  callId: string
  /** Callback depth when it was tracked: a report about it while it is unbuilt is one deeper. */
  depth: number
  /**
   * The async context it was tracked in, its request's: a report about it while it is unbuilt runs
   * there, as one about a queued event runs in its capture's (see Entry.context), whichever request
   * flushes. Held as long as the call is: while it is tracked, and by a flush that counted it.
   */
  context: AsyncResource
  done: Promise<void>
  finished: boolean
  /** Its event, once queued: kept here so a flush counts it even after it leaves the queue. */
  entry: Entry | undefined
}

/**
 * Holding back after `quota_exceeded`, until `until` (epoch ms) or a check finds the limit raised.
 * Events captured meanwhile are dropped at once, before any hook, except one every 15 minutes:
 * `check`, sent on its own to see whether the limit still holds.
 */
interface Holdback {
  until: number
  resetsAt: string
  /** The client's Clock after which the next event captured becomes the check. */
  nextCheck: number
  check: Entry | undefined
  dropped: number
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
  #redactKeys: string[] = []
  #onErrorHandler: LSquareOptions['onError']
  /**
   * The app's logger, or the default one, made only as the first line is written (see writeLog()):
   * a client given a logger never reads the runtime's `process` or `console` for one.
   */
  #logger: Logger | undefined
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
  #context: Context = { sdk: { name: SDK_NAME, version: SDK_VERSION, language: 'js' }, patterns: [], redactKeys: [], maxEventBytes: DEFAULTS.maxEventBytes }

  readonly #live = new Map<number, Entry>()
  #liveBytes = 0
  #zombies = 0
  #zombieBytes = 0
  #activeHooks = 0
  #activeRequests = 0
  /** Requests in flight, for reclaim(). */
  readonly #batches = new Set<Batch>()
  /** Events waiting out a retry delay, for reclaim(). */
  readonly #retrying = new Set<Entry>()
  #retryTokens = 0
  /** Cloudflare Workers, where requests are isolated from one another: see WATCH_MS. */
  readonly #isolated = isWorkers()
  /** Cloudflare Workers: events finished while their hook ran (see HOOK_BEAT_MS). */
  readonly #hung = new Set<Entry>()
  /** Events waiting for a request slot, in the order they became ready. */
  readonly #queued = new Set<Entry>()
  #pumpScheduled = false
  #holdback: Holdback | undefined
  readonly #waits = new Set<Wait>()
  #seq = 0
  #closed = false
  #reportedClosed = false
  /** Node: the later turns of reports and log lines still queued, in the order they were (see delivered()). */
  readonly #turns = new Map<Slot, () => void>()
  /** Its clock, for its limits, hooks, requests, retries, waits and quota checks alike, and the wrapper's: see Clock. */
  readonly #clock = new Clock()
  readonly #reportLimit = new RateLimit(this.#turns, this.#clock)
  readonly #logLimit = new RateLimit(this.#turns, this.#clock)
  #lastErrorLog = Number.NEGATIVE_INFINITY
  #suppressedErrors = 0
  /** Started by the first instrument(): before Node 24, an AsyncLocalStorage costs every promise. */
  #calls: AsyncLocalStorage<CallScope> | undefined
  #internal: Internal | undefined
  readonly #warned = new Set<string>()
  /** Calls on instrumented servers whose events are still being built (waiting for identify). */
  readonly #finishingCalls = new Map<string, FinishingCall>()

  /** Never throws. Without an `apiKey`, or with an invalid endpoint (outside dry run), the client is disabled. */
  constructor(options: LSquareOptions = {}) {
    try {
      // Read once each, never from Object.prototype: a polluted prototype can't supply an endpoint,
      // a key or `redact: false`. A config class's getters still work.
      const opts = readSettings(options, Object.keys(OPTION_KEYS)) as LSquareOptions
      if (opts.logger) {
        this.#logger = opts.logger
        this.#customLogger = !isConsole(opts.logger)
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
      const keys = readKeys(opts.redactKeys)
      if (!keys) {
        // Never sent without the masking the developer asked for.
        this.log('warn', '[lsquare] redactKeys must be a list of key names: capture is disabled.')
        return
      }
      if (keys.skipped) this.log('warn', "[lsquare] Part of redactKeys isn't a key name: it is ignored.")
      this.#redactKeys = keys.keys
      // The runtime's own only when the app gives none, read only then (see runtimeFetch()).
      this.#fetchImpl = opts.fetch ?? runtimeFetch()
      const runtime = runtimeName()
      this.#context = {
        sdk: { name: SDK_NAME, version: SDK_VERSION, language: 'js', ...(runtime ? { runtime } : {}) },
        patterns: this.#patterns,
        redactKeys: this.#redactKeys,
        maxEventBytes: this.#maxEventBytes,
      }
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
   * Sends a turn: what an agent did, outside an instrumented call. Never throws and never waits on
   * the network. Its synchronous work is bounded by the event's scan budget, except code supplied
   * with the input (getters, `toJSON`) and listing the keys of each object it reads, once per object
   * however often the input refers to it, which takes time in proportion to the object's size. Call
   * it as a method: detached from its client (e.g. `.then(lsquare.capture)`), it drops the event,
   * with nowhere to report it.
   */
  capture(input: CaptureInput): void {
    try {
      // Inside a call on an instrumented server, from the app's own code: the first capture()
      // before the tool returns is the call's agent part; any other is a turn linked to the call.
      const scope = currentDepth() === 0 ? this.#calls?.getStore() : undefined
      if (scope && isPlainObject(input)) {
        const fields = readFields(input, CAPTURE_FIELDS, true)
        if (!scope.returned && scope.agent === undefined) {
          // Taken once its top-level fields are read, before they're shaped: a capture() from the
          // app's code that shaping runs (a getter inside them, a toJSON) is a turn of its own. One
          // that a top-level field's getter ran came first, and may have taken it.
          const captured = capturedIds(fields)
          scope.agent = captured
          // The call's agent part, read now, as any capture()'s data is, not when the call's event
          // is built: what the app does with its objects afterwards changes nothing.
          if (this.#mode !== 'disabled') readAgent(captured, fields, this.#context)
          return
        }
        // Left out, null and an empty one alike are absent: linked to the call. A given ID, valid
        // or not (the literal 'empty' included), is kept for the checks.
        const given = idValue(fields.callId)
        if (given === undefined || given === 'empty') fields.callId = scope.callId
        const entry = this.enqueue('turn', fields)
        // Before the tool returns, its sending is the call's too: waited for with the call's event,
        // each capture on its own (two can share an event ID), until it is sent or dropped.
        const turns = scope.turns
        if (entry && !entry.outcome && !scope.returned && turns.size < MAX_CALL_TURNS) {
          turns.add(entry)
          ;(entry.waiters ??= []).push(() => turns.delete(entry))
        }
        return
      }
      this.enqueue('turn', input)
    } catch {
      try {
        this.report({ code: 'internal_error', message: 'capture() failed unexpectedly; the event was dropped.' })
      } catch {
        // Called detached, `this` is undefined. Throwing here would crash the app from a `.then()`.
      }
    }
  }

  /**
   * Inside a tool on an instrumented server: the call's ID, so a turn captured later elsewhere,
   * such as in a background job, can pass it as `callId`. Undefined outside a call.
   */
  currentCallId(): string | undefined {
    try {
      return this.#calls?.getStore()?.callId
    } catch {
      return undefined
    }
  }

  /** For the wrapper only: reached through `INTERNAL`. */
  get [INTERNAL](): Internal {
    this.#internal ??= {
      mode: this.#mode,
      record: (call, diagnostic) => {
        try {
          this.enqueue('tool_call', call, diagnostic)
        } catch {
          this.report({ code: 'internal_error', message: 'Recording a tool call failed unexpectedly; its event was dropped.' })
        }
      },
      calls: () => (this.#calls ??= new AsyncLocalStorage<CallScope>()),
      flushTimeoutMs: this.#flushTimeoutMs,
      maxConcurrentHooks: this.#maxConcurrentHooks,
      identifying: { active: 0 },
      identified: { keys: undefined },
      settled: (eventId, timeoutMs, turns = []) => {
        const pending = new Set<Entry>()
        for (const entry of this.#live.values()) {
          if (entry.eventId !== eventId) continue
          pending.add(entry)
          break
        }
        // Only the client's own entries, still queued: a turn is in the set until it is finished.
        for (const turn of turns) {
          const entry = turn as Entry
          if (this.#live.get(entry.seq) === entry) pending.add(entry)
        }
        if (pending.size === 0) return Promise.resolve()
        return new Promise<void>((resolve) => {
          const wait = Math.min(Math.max(0, timeoutMs), MAX_TIMER_MS)
          const context = this.#isolated ? new AsyncResource('LSquareWait') : undefined
          let stop = () => {}
          const done = () => {
            stop()
            clearTimeout(timer)
            resolve()
          }
          const timer = unref(setTimeout(done, wait))
          // Ended where it was made; on Workers, from another request, its watch ends it instead.
          for (const entry of pending) {
            ;(entry.waiters ??= []).push(() =>
              inContext(context, false, () => {
                pending.delete(entry)
                if (pending.size === 0) done()
              }),
            )
          }
          // One watch for them all, however many turns the call captured.
          if (context) {
            const left = () => [...pending].some((entry) => !entry.outcome)
            stop = this.watch(this.#clock.now() + wait, (candidate) => pending.has(candidate), () => (left() ? true : (done(), false)))
          }
        })
      },
      track: (callId, finishing) => {
        const call: FinishingCall = { callId, depth: currentDepth(), context: new AsyncResource('LSquareCall'), done: finishing, finished: false, entry: undefined }
        this.#finishingCalls.set(callId, call)
        void finishing
          .finally(() => {
            call.finished = true
            this.#finishingCalls.delete(callId)
          })
          .catch(() => {})
      },
      delivered: () => this.delivered(),
      flush: async (timeoutMs) => {
        await this.flush({ timeoutMs })
      },
      callback: (run) => {
        const depth = currentDepth() + 1
        const deeper = () => callbackDepth.run(depth, run)
        return this.#calls ? this.#calls.exit(deeper) : deeper()
      },
      log: (level, message) => this.log(level, message),
      warnOnce: (key, message) => {
        if (this.#warned.has(key)) return
        this.#warned.add(key)
        this.log('warn', message)
      },
      report: (error) => this.report(error),
      clock: this.#clock,
      validId,
      turnId: (value) => {
        const checked = turnIdValue(value)
        return valid(checked) ? checked.value : undefined
      },
      turnKey,
      closed: () => this.#closed,
    }
    return this.#internal
  }

  /**
   * Waits up to `timeoutMs` for the events captured before the call, except those whose next retry
   * is after the deadline: they can't finish in time. `0` doesn't wait; a `timeoutMs` it can't read
   * (a getter that throws) counts as not given. Never throws or rejects. Events still pending when
   * it returns are reported to `onError`, each in the request it was captured in, unless
   * `timeoutMs` is `0`: a call's still waiting for `identify` too, by the ID its event will have, in
   * the call's request (called from a callback, which doesn't wait for calls, the calls as it found
   * them). It resolves once what it reported, and the reports about the events it waited for, have
   * reached `onError` or the logger (on Node, not when called from one of them: see delivered()).
   */
  async flush(options?: { timeoutMs?: number | undefined }): Promise<FlushResult> {
    const result: FlushResult = { sent: 0, skipped: 0, failed: 0, pending: 0 }
    try {
      if (this.#mode === 'disabled') return result
      // Inside a call: its event isn't built yet, so it can't be waited for here. The wrapper sends
      // it before the reply instead, and this flush doesn't wait on itself.
      const scope = currentDepth() === 0 ? this.#calls?.getStore() : undefined
      if (scope && !scope.returned) scope.flushRequested = true
      let timeoutMs = waitTime(readTimeout(options), this.#flushTimeoutMs)
      // As asked: what is left pending is reported even when finishing calls took all the time.
      const waits = timeoutMs > 0
      const targets = [...this.#live.values()]
      // Calls that have replied but whose events wait for identify count too: waited for first,
      // their events then counted with the rest, even one already sent; one still unbuilt counts
      // as pending, one whose event was refused as failed. Not waited for from a callback
      // (identify itself, say): it could be waiting on its own call.
      const calls = [...this.#finishingCalls.values()]
      if (calls.length > 0 && timeoutMs > 0 && currentDepth() === 0) timeoutMs -= await this.finishCalls(calls, timeoutMs)
      const { unbuilt, dropped } = this.addCalls(targets, calls)
      result.pending += unbuilt.length
      result.failed += dropped
      this.pump()
      this.pumpRequests()
      if (targets.length > 0 && timeoutMs > 0) await this.waitFor(targets, timeoutMs)
      for (const entry of targets) {
        if (entry.outcome) {
          result[entry.outcome]++
        } else {
          result.pending++
          // With 0 the caller chose not to wait: pending is expected, not a problem.
          if (waits) {
            this.report(
              { code: 'pending_after_flush', message: 'Still pending when flush() returned.', eventId: entry.eventId },
              entry.redacted ? entry.event : undefined,
              entry.depth,
              entry.context,
              entry.bytes,
            )
          }
        }
      }
      // Pending too, by the ID its event will have: still waiting for identify. Reported in the
      // call's own request, at its depth, as an event's are.
      if (waits) {
        for (const call of unbuilt) {
          this.report({ code: 'pending_after_flush', message: 'Still pending when flush() returned.', eventId: call.callId }, undefined, call.depth, call.context)
        }
      }
    } catch {
      // flush() never rejects.
    }
    // A host waiting on it (waitUntil) may end the request once it resolves, taking pending turns
    // with it.
    await this.delivered()
    return result
  }

  /**
   * Terminal: drops events still waiting for `beforeSend`, waits up to `timeoutMs` for the rest
   * (`0`: not at all; never for a retry due after the deadline), then drops whatever is left,
   * aborting requests and retries. Later captures are dropped. By default it waits the flush
   * timeout, or 2 s when that is 0: a request path that doesn't wait still lets shutdown finish
   * what is in flight. Its result counts the events outstanding when it was called, one sent while
   * it waited for calls still being built included, and those it found as it closed. A `timeoutMs`
   * it can't read (a getter that throws) counts as not given, as flush()'s does: it still shuts
   * down. It resolves once what it reported has reached `onError` or the logger, as flush() does.
   */
  async shutdown(options?: { timeoutMs?: number | undefined }): Promise<FlushResult> {
    const result: FlushResult = { sent: 0, skipped: 0, failed: 0, pending: 0 }
    try {
      let timeoutMs = waitTime(readTimeout(options), this.#flushTimeoutMs || DEFAULTS.flushTimeoutMs)
      // Counted: the events outstanding now, as flush() counts them, even one sent while it waits
      // for the calls below, then those still live as it closes, each once.
      const before = [...this.#live.values()]
      // Calls whose events are still being built get the chance to be captured first; one still
      // unbuilt is dropped, so it counts as failed, as does one whose event was refused.
      const calls = [...this.#finishingCalls.values()]
      if (calls.length > 0 && timeoutMs > 0 && currentDepth() === 0) timeoutMs -= await this.finishCalls(calls, timeoutMs)
      this.#closed = true
      const targets = [...new Set([...before, ...this.#live.values()])]
      const { unbuilt, dropped } = this.addCalls(targets, calls)
      result.failed += unbuilt.length + dropped
      for (const entry of targets) {
        if (entry.stage === 'waiting' || entry.stage === 'hook') {
          this.finish(entry, 'failed', { code: 'shutdown', message: 'shutdown() dropped an event waiting for beforeSend.' })
        }
      }
      this.pumpRequests()
      const remaining = targets.filter((entry) => !entry.outcome)
      if (remaining.length > 0 && timeoutMs > 0) await this.waitFor(remaining, timeoutMs)
      for (const entry of [...this.#live.values()]) {
        this.finish(entry, 'failed', { code: 'shutdown', message: 'shutdown() ended before the event was sent.' })
      }
      for (const entry of targets) result[entry.outcome ?? 'failed']++
    } catch {
      // shutdown() never rejects.
    }
    // Its 'shutdown' reports too (see flush()). Disabled, it captured nothing, so it reported nothing.
    if (this.#mode !== 'disabled') await this.delivered()
    return result
  }

  /** Waits up to `timeoutMs` for these calls' events to be built. Returns the time spent. */
  private async finishCalls(calls: FinishingCall[], timeoutMs: number): Promise<number> {
    const started = this.#clock.now()
    let timer: ReturnType<typeof setTimeout> | undefined
    let timedOut = false
    await Promise.race([
      // On Workers, checked from here: another request's promise may continue in that request (see WATCH_MS).
      this.#isolated ? this.until(() => calls.every((call) => call.finished), timeoutMs) : Promise.all(calls.map((call) => call.done)),
      new Promise<void>((resolve) => (timer = setTimeout(() => ((timedOut = true), resolve()), timeoutMs))),
    ])
    clearTimeout(timer)
    // Its timer ran: all of it, even where the clock went back meanwhile unseen (see Clock).
    return timedOut ? timeoutMs : Math.min(timeoutMs, this.#clock.now() - started)
  }

  /** Resolves once `check()` holds, checked every WATCH_MS, or after `timeoutMs`. */
  private until(check: () => boolean, timeoutMs: number): Promise<void> {
    return new Promise<void>((resolve) => {
      const deadline = this.#clock.now() + timeoutMs
      const tick = () => {
        if (check() || this.#clock.now() >= deadline) resolve()
        else setTimeout(tick, WATCH_MS)
      }
      tick()
    })
  }

  /**
   * Adds the events these calls queued to `targets`, finished or not. Returns the calls still
   * unbuilt, and how many were dropped before they were queued (refused, or failed to build).
   */
  private addCalls(targets: Entry[], calls: FinishingCall[]): { unbuilt: FinishingCall[]; dropped: number } {
    const seen = new Set(targets)
    const unbuilt: FinishingCall[] = []
    let dropped = 0
    for (const call of calls) {
      if (call.entry) {
        if (!seen.has(call.entry)) targets.push(call.entry)
      } else if (call.finished) {
        dropped++
      } else {
        unbuilt.push(call)
      }
    }
    return { unbuilt, dropped }
  }

  /**
   * Returns the event's entry once it is queued, undefined when it isn't. `diagnostic` (a call's
   * identify_failed) is reported once, before what else is reported about the event, unless the
   * event is refused for shutdown(), which is reported instead (parity-25).
   */
  private enqueue(kind: 'turn' | 'tool_call', input: unknown, diagnostic?: LSquareError): Entry | undefined {
    if (this.#mode === 'disabled') return
    // Captured from code nested past the limit (e.g. a beforeSend capturing from a hook): refused,
    // so no capture chain can grow without bound.
    if (currentDepth() > MAX_CALLBACK_DEPTH) return
    if (this.#closed) {
      this.refuseClosed()
      return
    }
    let unsaid = diagnostic
    const say = () => {
      if (unsaid === undefined) return
      const said = unsaid
      unsaid = undefined
      this.report(said)
    }

    let check = false
    if (this.#holdback) {
      const admitted = this.admit(this.#holdback)
      if (admitted === 'drop') {
        say()
        return
      }
      check = admitted === 'check'
    }

    const hook = this.#beforeSend !== undefined
    let built: Built
    try {
      built =
        kind === 'turn'
          ? buildTurn(input, new Date().toISOString(), this.#context, hook)
          : buildCall(input as CallRecord, this.#context, hook)
    } catch {
      // Building ran the app's code (getters): one that shut the client down has the event refused
      // for that, reported once as shutdown, as one that was built is: neither its failure nor what
      // the call said of identify is reported (parity-25).
      if (this.#closed) {
        this.refuseClosed()
        return
      }
      say()
      this.report({ code: 'redaction_failed', message: 'Building the event failed; it was dropped, never sent unredacted.' })
      return
    }
    if (!built.ok) {
      // As above.
      if (this.#closed) {
        this.refuseClosed()
        return
      }
      say()
      const messages = {
        invalid_input: 'capture() needs a plain object; the event was dropped.',
        invalid_id: 'An ID must be a non-empty string of at most 512 bytes, without NUL; the event was dropped.',
        too_large: `The event is larger than ${this.#maxEventBytes} bytes even without its largest parts; it was dropped.`,
      }
      const error: LSquareError = { code: built.code, message: messages[built.code] }
      if (built.paths.length > 0) put(error, 'paths', built.paths)
      this.report(error)
      return
    }
    const { draft } = built
    const event = draft.event
    if (typeof event.userId === 'string' && !warnedContactUserId && looksLikeContact(event.userId)) {
      warnedContactUserId = true
      this.log('warn', '[lsquare] A userId looks like an email address or phone number. It is sent as given, so the person can be deleted by it: send an internal ID instead.')
    }
    // Waiting for its hook, it also holds what numbering needs across the hook.
    const bytes = byteSize(event) + (hook ? retainedBytes(draft) : 0)
    // Building ran the app's code (getters): one that shut the client down drops this event before
    // it can take the room of one shutdown() is still sending, as in Python.
    if (this.#closed) {
      this.refuseClosed()
      return
    }
    if (!this.makeRoom(bytes)) {
      // Refused for want of room, not for shutdown(): what the call says of identify holds.
      say()
      this.report({ code: 'queue_full', message: 'The queue is full; the event was dropped.', eventId: event.eventId })
      return
    }
    // Making room ran the app's code too (a stale check's abort listeners; an expired hook's run
    // later, from a timer): one that shut the client down drops this event as well.
    if (this.#closed) {
      this.refuseClosed()
      return
    }
    // Admitted: from here what the call says of its identify is true, said before the event's own
    // reports, from its later steps.
    say()

    const entry: Entry = {
      seq: ++this.#seq,
      depth: currentDepth(),
      context: new AsyncResource('LSquareEvent'),
      createdAt: this.#clock.now(),
      eventId: event.eventId,
      kind,
      event,
      draft: hook ? draft : undefined,
      bytes,
      stage: hook ? 'waiting' : 'ready',
      redacted: !hook,
      attempts: 0,
      retryAt: undefined,
      outcome: undefined,
      zombie: false,
      waiters: undefined,
      hookAbort: undefined,
      abortLater: false,
      slotGone: false,
      beat: 0,
      beats: undefined,
      batch: undefined,
      timer: undefined,
      retryToken: undefined,
      ignoredFields: draft.ignoredFields,
      ignoredToolCallFields: draft.notes.ignoredToolCallFields,
    }
    this.#live.set(entry.seq, entry)
    this.#liveBytes += bytes
    if (kind === 'tool_call') {
      const call = this.#finishingCalls.get(entry.eventId)
      if (call) call.entry = entry
    }
    if (check && this.#holdback) {
      this.#holdback.check = entry
      this.#holdback.nextCheck = this.#clock.now() + QUOTA_PROBE_MS
    }

    if (hook) {
      entry.timer = unref(setTimeout(() => this.hookDeadline(entry), this.#beforeSendTimeoutMs))
      queueMicrotask(() => this.pump())
    } else {
      // The closure holds only the entry, whose content is released if it's dropped before this runs.
      queueMicrotask(() => this.guard(entry, () => this.ready(entry)))
    }
    return entry
  }

  /** Reported once: an onError that captures after shutdown must not feed itself. */
  private refuseClosed(): void {
    if (this.#reportedClosed) return
    this.#reportedClosed = true
    this.report({ code: 'shutdown', message: 'capture() was called after shutdown(); this and later events are dropped.' })
  }

  /** Enforces hook deadlines and starts hooks for waiting events, within the concurrency limit. */
  private pump(scope?: Scope): void {
    try {
      if (!this.#beforeSend) return
      this.settleHooks()
      // One that can't start here is left to its own request (Workers), or to its deadline.
      let tries = 0
      for (const entry of this.candidates([...this.#live.values()].filter((entry) => entry.stage === 'waiting'), scope)) {
        if (this.#activeHooks >= this.#maxConcurrentHooks) break
        // A synchronous hook started above may have finished this one (a capture that made room, a
        // shutdown()), or started it (a flush(), whose pump runs inside it): only one still waiting.
        if (entry.stage !== 'waiting' || entry.outcome) continue
        if (this.startHook(entry, this.#beforeSend)) continue
        if (scope) scope.refused.add(entry)
        else if (++tries >= ENTER_TRIES) break
      }
    } catch {
      // Never let a background step throw into the app's event loop.
    }
  }

  /**
   * The events to try to start, in order: as they came, except on Workers, newest first (the
   * likeliest to be this request's), and for a waiting request only those it waits for and hasn't
   * already found it can't enter.
   */
  private candidates(entries: Entry[], scope: Scope | undefined): Entry[] {
    if (!this.#isolated) return entries
    return entries.reverse().filter((entry) => !scope || (scope.wanted(entry) && !scope.refused.has(entry)))
  }

  /**
   * Events past their hook deadline expire (where the deadline's timer went with its request, as on
   * Workers, nothing else would expire them), and hooks that went with their request give back
   * their slot and bytes.
   */
  private settleHooks(): void {
    for (const entry of [...this.#live.values()]) {
      if ((entry.stage === 'waiting' || entry.stage === 'hook') && this.pastDeadline(entry)) this.expire(entry)
    }
    if (this.#hung.size > 0) this.reclaimHooks()
  }

  /**
   * Workers: hooks that went with their request give back their slot and bytes (see HOOK_BEAT_MS).
   * One whose request is alive keeps beating, and keeps its slot until it settles, as everywhere.
   */
  private reclaimHooks(): void {
    const now = this.#clock.now()
    for (const entry of [...this.#hung]) {
      if (now - entry.beat < HOOK_GONE_MS) continue
      this.#hung.delete(entry)
      entry.slotGone = true
      entry.zombie = false
      entry.abortLater = false
      this.#activeHooks--
      this.#zombies--
      this.#zombieBytes -= entry.bytes
    }
  }

  /**
   * False when the event's context can't be entered from here (Workers: another request's): then
   * nothing is taken, and the event waits for its own request to start it, or for its deadline.
   */
  private startHook(entry: Entry, beforeSend: BeforeSend): boolean {
    // All in the async context of its capture(), whichever event's progress started it: the hook,
    // and the callback that starts the next one. Attached anywhere else, that callback would carry
    // another event's context along every handoff, keeping it reachable while hooks stay busy.
    return enter(entry.context, () => {
      let controller: AbortController
      let event: LSquareEvent
      try {
        // Before taking a slot: if these fail, no slot is ever left taken.
        controller = new AbortController()
        event = structuredClone(entry.event as LSquareEvent)
      } catch {
        this.finish(entry, 'failed', { code: 'internal_error', message: 'beforeSend could not be started; the event was dropped.' })
        return
      }
      entry.stage = 'hook'
      this.#activeHooks++
      entry.hookAbort = controller
      if (this.#isolated) {
        const clock = this.#clock
        const until = entry.createdAt + this.#beforeSendTimeoutMs + HOOK_BEAT_FOR_MS
        entry.beat = clock.now()
        entry.beats = setInterval(() => {
          entry.beat = clock.now()
          if (entry.beat < until) return
          clearInterval(entry.beats)
          entry.beats = undefined
        }, HOOK_BEAT_MS)
      }
      let pending: Promise<unknown>
      try {
        // The hook is a callback too: anything it captures is one level deeper than its event. A
        // native promise is watched as it is (Promise.resolve returns it), so the hook counts as
        // settled, for its abort, as soon as it is (see finish()); a subclass's, or any other
        // thenable, once adopted. Adopted at the hook's depth: the `then` that runs, in a job, is
        // the hook's own code, and captures as deep as the hook does.
        pending = callbackDepth.run(entry.depth + 1, () => Promise.resolve(beforeSend(event, { signal: controller.signal })))
      } catch (error) {
        pending = Promise.reject(error)
      }
      // As `await` follows it (see follow()): an own `then` of the hook's promise is never run here.
      follow(
        pending,
        (out) => this.hookDone(entry, out, false),
        () => this.hookDone(entry, undefined, true),
      ).catch(() => {})
    })
  }

  private hookDone(entry: Entry, out: unknown, failed: boolean): void {
    // In the hook's own context: this clears it.
    clearInterval(entry.beats)
    entry.beats = undefined
    if (entry.slotGone) {
      // Taken back already (see HOOK_BEAT_MS): nothing left to release.
      this.pump()
      return
    }
    this.#activeHooks--
    if (entry.zombie) {
      // Too late: the event was already dropped. Release what the hook held, discard its result.
      entry.zombie = false
      entry.abortLater = false
      this.#hung.delete(entry)
      this.clearTimer(entry)
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
        this.finishHook(entry, out)
      }
    })
    this.pump()
  }

  /** The hook's output, checked, shaped and redacted like the app's input: its fields are read one by one, never spread. */
  private finishHook(entry: Entry, out: Record<string, unknown>): void {
    let built: Built
    try {
      built = afterHook(out, entry.draft as Draft, this.#context)
    } catch {
      this.finish(entry, 'failed', { code: 'redaction_failed', message: 'Redaction failed; the event was dropped, not sent unredacted.' })
      return
    }
    entry.draft = undefined
    if (!built.ok) {
      this.finish(entry, 'failed', {
        code: built.code === 'too_large' ? 'too_large' : 'hook_failed',
        message:
          built.code === 'too_large'
            ? `beforeSend made the event larger than ${this.#maxEventBytes} bytes even without its largest parts; it was dropped.`
            : 'beforeSend returned invalid IDs; the event was dropped.',
      })
      return
    }
    const event = built.draft.event
    const bytes = byteSize(event)
    this.#liveBytes += bytes - entry.bytes
    entry.bytes = bytes
    entry.event = event
    entry.redacted = true
    if (!this.fitsAfterGrowth(entry)) {
      this.finish(entry, 'failed', { code: 'queue_full', message: 'The event grew past the memory limits; it was dropped.' })
    } else if (!entry.outcome) {
      // Not finished while making room: never dispatch an event already reported.
      this.ready(entry)
    }
  }

  /** Redacted and final: logged in dry run, otherwise queued for the next request with a free slot. */
  private ready(entry: Entry): void {
    if (entry.outcome) return
    if (this.#mode === 'dry-run') {
      this.log('info', dryRunSummary(entry), entry.depth, entry.context)
      this.finish(entry, 'skipped')
      return
    }
    this.dispatch(entry)
  }

  /** The event waits its turn in the order events became ready; requests go out in a later microtask, so events ready together go together. */
  private dispatch(entry: Entry): void {
    entry.stage = 'queued'
    this.#queued.add(entry)
    if (this.#pumpScheduled) return
    this.#pumpScheduled = true
    queueMicrotask(() => {
      this.#pumpScheduled = false
      this.pumpRequests()
    })
  }

  /**
   * Sends batches while a request slot is free: each carries the events ready now, up to the
   * ingestion's limits. The limit on slots keeps a slow or unreachable LSquare from holding many
   * sockets, and lets batches form under load.
   */
  private pumpRequests(scope?: Scope): void {
    try {
      this.reclaim()
      if (this.#holdback && !this.holdingBack()) return
      // Stops when nothing queued can start here (Workers: other requests' events, left to them).
      while (this.#activeRequests < this.#maxConcurrentRequests && this.#queued.size > 0 && this.startBatch(scope)) {}
    } catch {
      // Never let a background step throw into the app's event loop.
    }
  }

  /**
   * Starts a request for a queued event whose context can be entered from here (the first, except
   * on Workers: see candidates()), carrying the other events queued, those of requests that ended
   * included. False when none can.
   */
  private startBatch(scope?: Scope): boolean {
    let tries = 0
    for (const first of this.candidates([...this.#queued], scope)) {
      if (enter(first.context, () => this.sendBatch(this.takeBatch(first), false))) return true
      if (scope) scope.refused.add(first)
      else if (++tries >= ENTER_TRIES) return false
    }
    return false
  }

  /** Whether an event captured while a daily limit holds goes ahead, becomes the check, or is dropped and counted. */
  private admit(holdback: Holdback): 'send' | 'check' | 'drop' {
    if (Date.now() >= holdback.until) {
      this.endHoldback()
      return 'send'
    }
    const check = holdback.check
    if (check && this.#clock.now() >= holdback.nextCheck) {
      // Unanswered for a whole interval (on Workers, its request may have ended, and no other can
      // send it): dropped and counted, and this event checks instead.
      holdback.check = undefined
      holdback.dropped++
      this.finish(check, 'failed')
    }
    if (holdback.check === undefined && this.#clock.now() >= holdback.nextCheck) return 'check'
    holdback.dropped++
    return 'drop'
  }

  /**
   * While a daily limit holds, events that became ready (captured before it, or held by their hook)
   * are dropped and counted, and the check goes on its own. True once the limit has reset.
   */
  private holdingBack(): boolean {
    const holdback = this.#holdback as Holdback
    if (Date.now() >= holdback.until) {
      this.endHoldback()
      return true
    }
    for (const entry of [...this.#queued]) {
      if (entry === holdback.check) continue
      holdback.dropped++
      this.finish(entry, 'failed')
    }
    const check = holdback.check
    if (check && this.#queued.has(check) && this.#activeRequests < this.#maxConcurrentRequests) {
      enter(check.context, () => {
        this.#queued.delete(check)
        this.sendBatch([check], true)
      })
    }
    return false
  }

  /**
   * Starts holding back, or extends it, until `resetsAt`. A `resetsAt` already past means the limit
   * has reset since: a late answer, which starts nothing. False then.
   */
  private startHoldback(resetsAt: string | undefined): boolean {
    const parsed = resetsAt === undefined ? Number.NaN : Date.parse(resetsAt)
    if (Number.isFinite(parsed) && parsed <= Date.now()) return false
    const until = resetTime(resetsAt)
    const at = new Date(until).toISOString()
    const holdback = this.#holdback
    if (holdback) {
      if (until > holdback.until) {
        holdback.until = until
        holdback.resetsAt = at
      }
      return true
    }
    this.#holdback = { until, resetsAt: at, nextCheck: this.#clock.now() + QUOTA_PROBE_MS, check: undefined, dropped: 0 }
    this.report({
      code: 'quota_exceeded',
      message: `The project's daily limit was reached; events are dropped until ${at.slice(11, 16)} UTC.`,
      resetsAt: at,
    })
    return true
  }

  private endHoldback(): void {
    const holdback = this.#holdback
    this.#holdback = undefined
    if (holdback && holdback.dropped > 0) {
      this.report({
        code: 'quota_dropped',
        message: `Sending resumed after the daily limit; ${holdback.dropped} events were dropped.`,
        count: holdback.dropped,
      })
    }
  }

  /**
   * `first`, then the other events ready now, in order, within the ingestion's limits: 100 events,
   * 1 MB of body, and an acknowledgement that stays under 60 KiB even if every event came back as
   * `quota_exceeded`.
   */
  private takeBatch(first: Entry): Entry[] {
    const batch: Entry[] = [first]
    let body = BATCH_OVERHEAD + first.bytes + 1
    let ack = ACK_OVERHEAD + ackBytes(first.eventId)
    for (const entry of this.#queued) {
      if (entry === first) continue
      const eventAck = ackBytes(entry.eventId)
      const fits = batch.length < MAX_BATCH_EVENTS && body + entry.bytes + 1 <= MAX_BATCH_BYTES && ack + eventAck <= MAX_ACK_BYTES
      if (!fits) break
      batch.push(entry)
      body += entry.bytes + 1
      ack += eventAck
    }
    for (const entry of batch) this.#queued.delete(entry)
    return batch
  }

  /**
   * One request for the batch, called in the async context of its first event, which the request
   * runs in. Every attempt ends by the request timeout, so the slot always returns: by its own
   * timer, or by reclaim() where that timer went with a request that ended (Workers).
   */
  private sendBatch(entries: Entry[], probe: boolean): void {
    const batch: Batch = {
      live: entries.length,
      controller: new AbortController(),
      probe,
      holdback: probe ? this.#holdback : undefined,
      entries,
      context: entries[0]?.context,
      deadline: this.#clock.now() + this.#requestTimeoutMs,
      ended: false,
    }
    for (const entry of entries) {
      entry.stage = 'sending'
      entry.batch = batch
      entry.attempts++
    }
    this.#activeRequests++
    this.#batches.add(batch)
    void this.send(entries, batch).finally(() => {
      this.endBatch(batch)
      this.pumpRequests()
    })
  }

  /** Returns the batch's slot, once: whichever comes first, its own end or reclaim(). */
  private endBatch(batch: Batch): boolean {
    if (batch.ended) return false
    batch.ended = true
    this.#batches.delete(batch)
    this.#activeRequests--
    return true
  }

  private async send(entries: Entry[], batch: Batch): Promise<void> {
    /**
     * The events still this request's once its exchange is over: none it finished meanwhile, and none
     * if reclaim() took the batch over, so a late answer finishes, retries or holds back nothing.
     */
    let owned: Entry[] | undefined
    try {
      const { controller } = batch
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
      /** The fetch, until it has answered in time: one that answers after the timeout, ignoring the abort, has its body stopped. */
      let fetching: Promise<Response> | undefined
      try {
        // Through a local: called as a method, the client would be its `this`.
        const fetchImpl = this.#fetchImpl as typeof fetch
        // Adopted as `await` adopts it (see follow()): an own `then` of a custom fetch's promise is
        // never run by the race below, at once.
        fetching = follow(
          Promise.resolve(
            fetchImpl(this.#url, {
              method: 'POST',
              headers: { 'content-type': 'application/json', authorization: `Bearer ${this.#apiKey}` },
              body: JSON.stringify({ events: entries.map((entry) => entry.event) }),
              signal: controller.signal,
              // Not 'error', which Workers' fetch rejects. A redirect is never followed, so the key
              // can't go elsewhere.
              redirect: 'manual',
            }),
          ),
        ) as Promise<Response>
        const response: unknown = await Promise.race([fetching, expired])
        fetching = undefined
        // Each field read once, and silenced: a custom fetch may answer with getters of its own.
        const status = member(response, 'status') as number
        const body = member(response, 'body')
        if ((status >= 300 && status < 400) || status === 0 || member(response, 'type') === 'opaqueredirect') {
          // Any 3xx, or a browser's opaque redirect: refused, and retried as a network error, as in Python.
          discard(body)
          throw new Error('redirect')
        }
        const ok = status >= 200 && status < 300
        // Only a 2xx body matters (the acknowledgement). Others are decided from status and headers.
        if (!ok) discard(body)
        reply = {
          status,
          retryAfter: parseRetryAfter(header(response, 'retry-after')),
          body: ok ? await Promise.race([readBody(body, MAX_RESPONSE_BYTES, controller.signal), expired]) : '',
        }
      } catch {
        // Network error, timeout or refused redirect. Retrying is safe: event IDs are stable.
        if (fetching) discardLate(fetching)
      } finally {
        clearTimeout(timeout)
        owned = entries.filter((entry) => entry.batch === batch)
        for (const entry of owned) entry.batch = undefined
      }
      const live = owned.filter((entry) => !entry.outcome)
      if (!reply) {
        this.retryOrFail(live, undefined, undefined)
        return
      }

      const { status } = reply
      if (status >= 200 && status < 300) {
        const acks = reply.body === undefined ? undefined : acknowledgements(reply.body)
        const overQuota: Entry[] = []
        let resetsAt: string | undefined
        let accepted = 0
        for (const entry of live) {
          const ack = acks ? (acks.get(entry.eventId) ?? 'unexpected') : 'unexpected'
          if (ack === 'accepted') {
            accepted++
            this.finish(entry, 'sent')
          } else if (ack === 'unexpected') {
            this.finish(entry, 'failed', { code: 'send_failed', message: 'Unexpected response: LSquare did not acknowledge the event.', status })
          } else if (ack.rejected === 'quota_exceeded') {
            resetsAt ??= ack.resetsAt
            overQuota.push(entry)
          } else {
            this.finish(entry, 'failed', { code: 'rejected', message: `LSquare rejected the event: ${ack.rejected}.` })
          }
        }
        if (overQuota.length > 0) {
          // Final, never retried. Holding back, they're counted and reported once; after a late
          // answer from a limit that has reset since, each is reported.
          const holding = this.startHoldback(resetsAt)
          for (const entry of overQuota) {
            if (holding && this.#holdback) {
              this.#holdback.dropped++
              this.finish(entry, 'failed')
            } else {
              this.finish(entry, 'failed', { code: 'rejected', message: 'LSquare rejected the event: quota_exceeded.' })
            }
          }
        } else if (batch.probe && accepted > 0 && this.#holdback && this.#holdback === batch.holdback) {
          this.endHoldback()
        }
      } else if (status === 429 || status >= 500) {
        this.retryOrFail(live, status, reply.retryAfter)
      } else {
        for (const entry of live) this.finish(entry, 'failed', { code: 'send_failed', message: `LSquare answered ${status}; not retried.`, status })
      }
    } catch {
      for (const entry of owned ?? entries.filter((entry) => entry.batch === batch)) {
        if (!entry.outcome) this.finish(entry, 'failed', { code: 'internal_error', message: 'Sending failed unexpectedly.' })
      }
    }
  }

  /** Each event retries on its own count; one delay for the batch, so its events go out together again. */
  private retryOrFail(entries: Entry[], status: number | undefined, retryAfterMs: number | undefined): void {
    const retrying: Entry[] = []
    for (const entry of entries) {
      if (entry.attempts < this.#maxAttempts) {
        retrying.push(entry)
        continue
      }
      const error: LSquareError = {
        code: 'send_failed',
        message:
          status === undefined
            ? `Network error after ${entry.attempts} attempts.`
            : `LSquare answered ${status} after ${entry.attempts} attempts.`,
      }
      if (status !== undefined) put(error, 'status', status)
      this.finish(entry, 'failed', error)
    }
    if (retrying.length === 0) return
    const attempts = Math.max(...retrying.map((entry) => entry.attempts))
    const backoff = Math.random() * Math.min(RETRY_CAP_MS, RETRY_BASE_MS * 2 ** (attempts - 1))
    const delay = retryAfterMs === undefined ? backoff : Math.min(retryAfterMs, RETRY_AFTER_CAP_MS)
    const retryAt = this.#clock.now() + delay
    for (const entry of retrying) {
      const token = ++this.#retryTokens
      entry.stage = 'retry'
      entry.retryAt = retryAt
      entry.retryToken = token
      this.#retrying.add(entry)
      entry.timer = unref(setTimeout(() => this.retryNow(entry, token), delay))
      // A flush whose deadline comes first can't see this event finish: it stops waiting for it.
      this.release(entry)
    }
  }

  /**
   * Its retry is due: from its timer, or from reclaim() where the timer went with a request that
   * ended (Workers). Only the retry still current goes; an older one's timer finds nothing to do.
   */
  private retryNow(entry: Entry, token: number): void {
    if (entry.retryToken !== token) return
    entry.retryToken = undefined
    this.#retrying.delete(entry)
    this.clearTimer(entry)
    entry.retryAt = undefined
    if (entry.outcome) return
    this.dispatch(entry)
  }

  private pastDeadline(entry: Entry): boolean {
    return this.#clock.now() - entry.createdAt >= this.#beforeSendTimeoutMs
  }

  private timeoutError(): LSquareError {
    return { code: 'hook_timeout', message: `beforeSend did not finish within ${this.#beforeSendTimeoutMs} ms; the event was dropped.` }
  }

  private expire(entry: Entry): void {
    if (entry.outcome || (entry.stage !== 'waiting' && entry.stage !== 'hook')) return
    this.finish(entry, 'failed', this.timeoutError())
  }

  /** The hook's timer, in the event's own context: expires it, or delivers an abort finish() left to it. */
  private hookDeadline(entry: Entry): void {
    if (!this.abortLate(entry)) this.expire(entry)
  }

  /**
   * Aborts a hook whose event was finished while it ran, unless it has settled since. On Workers,
   * from a microtask: a hook that settles in the microtasks before it is still aborted.
   */
  private abortLate(entry: Entry): boolean {
    if (!entry.abortLater) return false
    entry.abortLater = false
    abort(entry.hookAbort)
    return true
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
    // On Workers, hooks that went with their request pin nothing any more: freed before the event is weighed.
    if (this.#isolated && this.#beforeSend) this.settleHooks()
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
    if (this.#isolated && this.#beforeSend) this.settleHooks()
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
    this.#retrying.delete(entry)
    entry.retryToken = undefined
    const batch = entry.batch
    entry.batch = undefined
    // Its request carries other events too: aborted only once none of them still needs it.
    const lastOfBatch = batch !== undefined && --batch.live === 0
    const hook = entry.stage === 'hook'
    const context = entry.context
    if (hook) {
      entry.zombie = true
      this.#zombies++
      this.#zombieBytes += entry.bytes
    }
    this.#live.delete(entry.seq)
    this.#queued.delete(entry)
    this.#liveBytes -= entry.bytes
    if (this.#holdback?.check === entry) this.#holdback.check = undefined
    // The report's turn is queued before anything waiting for the event wakes, so a wait that then
    // waits for the turns queued (delivered()) ends after it.
    if (error) this.report({ eventId: entry.eventId, ...error }, entry.redacted ? entry.event : undefined, entry.depth, entry.context, entry.bytes)
    const waiters = entry.waiters
    entry.waiters = undefined
    for (const waiter of waiters ?? []) {
      try {
        waiter()
      } catch {
        // Ours, and never throws; guarded all the same.
      }
    }
    // The report took its own copy, and its own reference to the context. Whatever still holds this
    // entry (a pending microtask, a flush, a slow fetch, a hook that ignored its signal) holds no
    // content and no request context from here.
    entry.event = undefined
    entry.draft = undefined
    entry.context = undefined
    this.release(entry)
    // Last, once the client's books are straight: a signal's listeners are the app's, and may capture
    // or flush. Each is aborted in the context its listeners belong to; where that can't be entered
    // from here (Workers: another request's), a request's own timeout ends it.
    if (lastOfBatch) abortIn(batch.context, batch.controller)
    if (hook) {
      // A running hook's signal is aborted later, in its own context, and only if the hook is still
      // running then: one that has returned leaves stage 'hook' only when hookDone runs, a microtask
      // later, so the app's code chained on its completion (a shutdown()) can finish it first.
      // hookDone clears abortLater. A timer comes after every microtask; on Workers it may go with the
      // request that ends as this event is finished, so there a microtask instead, which still comes
      // after the hookDone of a hook that has already returned (its reaction is queued first). Where
      // the hook's context can't be entered from here, its deadline timer, in that context, delivers
      // the abort instead.
      entry.abortLater = true
      enter(context, () => {
        this.clearTimer(entry)
        if (this.#isolated) queueMicrotask(() => this.abortLate(entry))
        else entry.timer = unref(setTimeout(() => this.hookDeadline(entry), 0))
      })
    } else {
      this.clearTimer(entry)
    }
    if (hook && this.#isolated) this.#hung.add(entry)
  }

  /**
   * Waits up to `timeoutMs` for `entries`. One whose next attempt is due after the deadline can't
   * finish in time, so it isn't waited for. The timer is not unref'd: an awaited flush keeps the
   * process alive until it returns.
   */
  private waitFor(entries: Entry[], timeoutMs: number): Promise<void> {
    return new Promise<void>((resolve) => {
      const deadline = this.#clock.now() + timeoutMs
      const wait: Wait = { deadline, pending: new Set(entries.filter((entry) => !settledBy(entry, deadline))), end: resolve, context: undefined }
      if (wait.pending.size === 0) return resolve()
      const timer = setTimeout(() => wait.end(), timeoutMs)
      let stop = () => {}
      wait.end = () => {
        stop()
        clearTimeout(timer)
        this.#waits.delete(wait)
        resolve()
      }
      this.#waits.add(wait)
      if (this.#isolated) {
        wait.context = new AsyncResource('LSquareWait')
        stop = this.watch(deadline, (entry) => wait.pending.has(entry), () => (wait.pending.size > 0 ? true : (wait.end(), false)))
      }
    })
  }

  /**
   * Cloudflare Workers: every WATCH_MS until `deadline`, in the waiting request, the pumps start
   * what of the `wanted` events can start there, then `check()` says whether to go on (false once
   * the wait has ended). Returns what stops it.
   */
  private watch(deadline: number, wanted: (entry: Entry) => boolean, check: () => boolean): () => void {
    let timer: ReturnType<typeof setTimeout> | undefined
    let stopped = false
    const scope: Scope = { wanted, refused: new WeakSet() }
    const tick = () => {
      timer = undefined
      if (stopped) return
      this.pump(scope)
      this.pumpRequests(scope)
      if (stopped || !check()) return
      const left = deadline - this.#clock.now()
      if (left > 0) timer = setTimeout(tick, Math.min(WATCH_MS, left))
    }
    timer = setTimeout(tick, Math.min(WATCH_MS, Math.max(0, deadline - this.#clock.now())))
    return () => {
      stopped = true
      // From another request, a no-op: the tick then finds it stopped.
      clearTimeout(timer)
    }
  }

  /**
   * What a request that ended took with it (Workers: its fetches and timers): a batch still unsettled
   * at its deadline gives its slot back, its events treated as after a timeout, and a retry whose time
   * has come goes. Whoever notices first does it; the batch's or retry's own timer, where it still
   * runs, then finds nothing left to do. Waits past their deadline are let go.
   */
  private reclaim(): void {
    const now = this.#clock.now()
    for (const batch of [...this.#batches]) {
      if (now < batch.deadline || !this.endBatch(batch)) continue
      abortIn(batch.context, batch.controller)
      const live = batch.entries.filter((entry) => entry.batch === batch && !entry.outcome)
      for (const entry of live) entry.batch = undefined
      this.retryOrFail(live, undefined, undefined)
    }
    for (const entry of [...this.#retrying]) {
      if (entry.retryToken !== undefined && entry.retryAt !== undefined && entry.retryAt <= now) this.retryNow(entry, entry.retryToken)
    }
    // Not ended from here: a request Workers cancelled never ran its timer, and one alive ends its own.
    for (const wait of [...this.#waits]) if (now >= wait.deadline) this.#waits.delete(wait)
  }

  /** Lets waits stop waiting for an event that finished, or whose next attempt is after their deadline. */
  private release(entry: Entry): void {
    for (const wait of this.#waits) {
      if (!wait.pending.has(entry) || !settledBy(entry, wait.deadline)) continue
      wait.pending.delete(entry)
      // On Workers only in its own request: from another, its watch ends it.
      if (wait.pending.size === 0) inContext(wait.context, false, wait.end)
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
   * can't change what is sent. The handler runs in a later event-loop turn, or from a microtask of
   * a flush() or shutdown() that runs it first (see delivered()), so it can't re-enter the SDK in
   * the middle of a step (capturing from `onError` is safe). The report holds a slot of its rate
   * limit until the handler returns, or settles if it's async; past the bytes those slots may hold,
   * it goes without the copy. `bytes` is the event's, as the queue counts them.
   *
   * `origin` is the depth of the event the report is about. Only it counts, not whatever code
   * happens to be running: a hook freed by deep callback code must not make another event's
   * reports deep too. Reports not about a queued event use the current depth.
   */
  private report(error: LSquareError, event?: LSquareEvent, origin = currentDepth(), context?: AsyncResource, bytes?: number): void {
    const limit = this.#reportLimit
    let slot: Slot | undefined
    try {
      const depth = origin + 1
      if (depth > MAX_CALLBACK_DEPTH) return
      // Only a custom handler sees the event, so only then is it copied.
      slot = limit.take(this.#onErrorHandler && event !== undefined ? bytes : undefined, this.#maxQueueBytes)
      if (!slot) return
      const delivered = slot.suppressed > 0 ? { ...error, suppressed: slot.suppressed } : error
      const snapshot = slot.bytes !== undefined ? structuredClone(event) : undefined
      // A custom handler or logger only in the event's context (another request's would see the wrong app state); the default log anywhere.
      this.later(limit, slot, context, !this.#onErrorHandler && !this.#customLogger, (release) => this.deliver(delivered, snapshot, depth, release))
      slot = undefined
    } catch {
      // onError must never break the app.
    } finally {
      if (slot) limit.release(slot, true)
    }
  }

  /** True when an async `onError` or logger now holds the report's slot, and calls `release` once it settles. */
  private deliver(error: LSquareError, event: LSquareEvent | undefined, depth: number, release: () => void): boolean {
    try {
      const handler = this.#onErrorHandler
      if (handler) return callbackDepth.run(depth, () => swallow(handler(error, event), release))
      // Reports the rate limit dropped before they got here count as suppressed too.
      this.#suppressedErrors += error.suppressed ?? 0
      // A clock that can't be read counts as long ago: the line is logged.
      const now = performanceNow(Number.NEGATIVE_INFINITY)
      // One logged later than now was logged on a clock that has gone back since (see RateLimit.take): long ago.
      if (now >= this.#lastErrorLog && now - this.#lastErrorLog < ERROR_LOG_INTERVAL_MS) {
        this.#suppressedErrors++
        return false
      }
      const more = this.#suppressedErrors > 0 ? ` (${this.#suppressedErrors} more since the last report)` : ''
      this.#lastErrorLog = now
      this.#suppressedErrors = 0
      return this.writeLog('warn', `[lsquare] ${error.code}: ${error.message}${more}`, depth, release)
    } catch {
      // onError must never break the app.
      return false
    }
  }

  /** Logs in a later turn, within the depth and rate limits, so a logger can't re-enter the SDK mid-step. */
  private log(level: 'info' | 'warn', message: string, origin = currentDepth(), context?: AsyncResource): void {
    const limit = this.#logLimit
    let slot: Slot | undefined
    try {
      const depth = origin + 1
      if (depth > MAX_CALLBACK_DEPTH) return
      slot = limit.take()
      if (!slot) return
      const line = slot.suppressed > 0 ? `${message} [lsquare: ${slot.suppressed} earlier log lines suppressed]` : message
      this.later(limit, slot, context, !this.#customLogger, (release) => this.writeLog(level, line, depth, release))
      slot = undefined
    } catch {
      // Ignore.
    } finally {
      if (slot) limit.release(slot, true)
    }
  }

  /**
   * Runs a report or log line in a later turn, in `context` (see inContext). It holds `slot` until
   * `run` returns, or, when `run` returns true, until the async handler it started settles and calls
   * `release`. One that never runs gives its slot back as refused; on Workers, one whose turn went
   * with its request gives it back HOOK_GONE_MS later (see RateLimit.reclaim), as does an async
   * handler whose request ended while it ran (see RateLimit.hold).
   */
  private later(limit: RateLimit, slot: Slot, context: AsyncResource | undefined, anywhere: boolean, run: (release: () => void) => boolean): void {
    const isolated = this.#isolated
    // Node: delivered() may run it first, from another request's flush(): it runs in the context it
    // was queued in all the same, and its own turn is taken off the queue.
    if (!context && !isolated) context = new AsyncResource('LSquareLater')
    const job: LaterJob = { limit, slot, context, anywhere, isolated, run, cancel: undefined }
    const turn = laterTurn(job)
    const immediate = setImmediate(turn)
    // Node only, where delivered() may run it first.
    if (!isolated) job.cancel = unschedule(immediate)
    // Counted once queued, so a turn that couldn't be scheduled never is (see delivered()).
    limit.wait(slot, isolated, turn)
  }

  /**
   * Settles once the reports and log lines queued so far have had their later turn: their handlers
   * have been called, though an async one's own I/O may go on. Whatever a host waits on (Workers'
   * or Vercel's waitUntil, an awaited flush() or shutdown()) ends through it, since a request that
   * ends takes its pending turns with it. At once when no turn is to come. Never rejects.
   *
   * On Node it runs those turns itself, so it waits for no macrotask, which an app's fake timers
   * would hold, or throw away (useRealTimers()) and leave their count up for good. It does so from
   * a microtask, not there and then: flush() may be called inside capture() (from an input's
   * getter), where no handler may run. Not from a callback (an onError that calls flush()): it
   * waits for nothing then, and the others keep their own turns. On Workers, where a turn queued
   * in another request can't run in this one, it waits one turn, after theirs (setImmediate runs
   * them in order); one that went with its request is taken back (see RateLimit.reclaim).
   */
  private delivered(): Promise<void> {
    if (this.#reportLimit.pending() + this.#logLimit.pending() === 0) return Promise.resolve()
    if (this.#isolated) {
      return new Promise<void>((resolve) => {
        try {
          setImmediate(resolve)
        } catch {
          resolve()
        }
      })
    }
    if (currentDepth() > 0) return Promise.resolve()
    return Promise.resolve().then(() => {
      try {
        // Those queued so far, in order (each holds a slot, so at most a limit's slots of each
        // kind), each in its own context and once (RateLimit.start). What their handlers queue
        // keeps its own turn.
        for (const turn of [...this.#turns.values()]) turn()
      } catch {
        // Never rejects.
      }
    })
  }

  /**
   * Logging must never break the app, even with a logger that throws or returns a rejected promise.
   * True when an async logger now holds the line's slot, and calls `release` once it settles.
   */
  private writeLog(level: 'info' | 'warn', message: string, depth: number, release: () => void): boolean {
    try {
      const logger = (this.#logger ??= defaultLogger())
      const write = () => {
        // Read once, and silenced: a getter may give a rejected promise, which is no method. The
        // logger's own or its class's, never Object.prototype's: a logger without `info` drops
        // info lines, which a polluted one mustn't get.
        const method: unknown = definedBelowObjectPrototype(logger, level) ? logger[level] : undefined
        silence(method)
        return typeof method === 'function' && swallow(Reflect.apply(method, logger, [message]), release)
      }
      // The default logger can't call back into the SDK, so it needs no depth (see callbackDepth).
      if (this.#customLogger) return callbackDepth.run(depth, write)
      return write()
    } catch {
      // Ignore.
      return false
    }
  }
}

/**
 * Catches the rejection of whatever thenable a callback returned, and calls `settled` once it
 * settles. False, without calling it, when there's nothing to wait for. The answer is silenced,
 * and a thenable adopted as `await` adopts one (see adopted()): its `then` read once, to tell, and
 * that one run.
 */
function swallow(value: unknown, settled?: () => void): boolean {
  try {
    const adopting = adopted(value)
    if (adopting === undefined) return false
    const done = adopting.then(
      () => {},
      () => {},
    )
    if (settled) void done.then(settled)
    return true
  } catch {
    // A throwing `then` getter: nothing to wait for.
  }
  return false
}

/** A report's or log line's later turn, and all it needs to run (see LSquare.later()). */
interface LaterJob {
  limit: RateLimit
  slot: Slot
  /** Where the handler runs (see inContext), and whether it may run anywhere else. */
  context: AsyncResource | undefined
  anywhere: boolean
  /** Workers: an async handler holds its slot by its beat (see RateLimit.hold). */
  isolated: boolean
  run: (release: () => void) => boolean
  /**
   * Node: takes its own immediate back, when delivered() runs it first. It holds that immediate,
   * which holds the request it was queued in: let go of with the job.
   */
  cancel: (() => void) | undefined
}

/**
 * The later turn of `job`: it holds the job alone, outside the client, and lets go of it as it
 * starts, whichever way, wherever the turn itself still is (an app's fake clock, or instrumentation
 * that keeps every callback it was given, may hold it for good). So no event copy, request or
 * client stays alive through it past its slot.
 */
function laterTurn(job: LaterJob): () => void {
  let queued: LaterJob | undefined = job
  return () => {
    const claimed = queued
    queued = undefined
    // Run already (delivered()), or taken back while it waited, its count gone to the next one.
    if (claimed && claimed.limit.start(claimed.slot)) runLater(claimed)
  }
}

/**
 * Runs a later turn's handler. It holds its slot until it returns, or, when `run` returns true,
 * until the async handler it started settles (on Workers, or its beat stops: see RateLimit.hold).
 * One that never ran gives its slot back as refused.
 */
function runLater(job: LaterJob): void {
  const { limit, slot } = job
  let ran = false
  let held = false
  try {
    job.cancel?.()
    inContext(job.context, job.anywhere, () => {
      ran = true
      held = job.run(releaser(limit, slot))
      if (held && job.isolated) limit.hold(slot)
    })
  } catch {
    // Ignore.
  } finally {
    if (!held) limit.release(slot, !ran)
  }
}

/** What an async handler calls once it settles: it holds the slot and its limit, nothing else. */
function releaser(limit: RateLimit, slot: Slot): () => void {
  return () => limit.release(slot)
}

/**
 * Takes `immediate` back, with the clearImmediate of the clock that queued it (an app's fake one,
 * maybe). Outside the turn's own scope, which would otherwise hold it.
 */
function unschedule(immediate: ReturnType<typeof setImmediate>): () => void {
  const clear = clearImmediate
  return () => clear(immediate)
}

/** Finished, or not due to try again before `deadline`. */
function settledBy(entry: Entry, deadline: number): boolean {
  return entry.outcome !== undefined || (entry.retryAt !== undefined && entry.retryAt >= deadline)
}

/**
 * Runs `run` in `context` and returns true; or returns false, having run nothing, when the context
 * can't be entered from here: on Cloudflare Workers, one made in another request (runInAsyncScope
 * throws before calling `run`). An error thrown by `run` itself is thrown on.
 */
function enter(context: AsyncResource | undefined, run: () => void): boolean {
  if (!context) {
    run()
    return true
  }
  let entered = false
  try {
    context.runInAsyncScope(() => {
      entered = true
      run()
    })
  } catch (error) {
    if (entered) throw error
    return false
  }
  return true
}

/** In `context`; where it can't be entered (Workers: another request's), here if `anywhere`, else not at all. */
function inContext(context: AsyncResource | undefined, anywhere: boolean, run: () => void): void {
  if (!enter(context, run) && anywhere) run()
}

/** Aborts in the context the signal's listeners belong to. False when it can't be entered from here: nothing was aborted. */
function abortIn(context: AsyncResource | undefined, controller: AbortController | undefined): boolean {
  try {
    return enter(context, () => abort(controller))
  } catch {
    return true
  }
}

function abort(controller: AbortController | undefined): void {
  try {
    controller?.abort()
  } catch {
    // Ignore.
  }
}

/**
 * Internal.validId. Outside the client, so what holds it (what waits on an `identify` that runs on)
 * doesn't hold the client.
 */
function validId(value: unknown): boolean {
  return typeof idValue(value) === 'object'
}

/** What one event can add to an acknowledgement: its ID with the longest rejection, and a comma. */
function ackBytes(eventId: string): number {
  return byteSize({ eventId, reason: 'quota_exceeded', resetsAt: LONGEST_RESETS_AT }) + 1
}

/** When a daily limit resets: `resetsAt` if it's in the next 25 hours, else the next 00:00 UTC. */
function resetTime(resetsAt: string | undefined): number {
  const now = Date.now()
  const parsed = resetsAt === undefined ? Number.NaN : Date.parse(resetsAt)
  if (Number.isFinite(parsed) && parsed > now && parsed <= now + 25 * 3_600_000) return parsed
  const next = new Date(now)
  next.setUTCHours(24, 0, 0, 0)
  return next.getTime()
}

/** One line per event: shape only, never content. */
function dryRunSummary(entry: Entry): string {
  // Called while the entry is live, before it is finished and released.
  const event = entry.event as LSquareEvent
  // No prototype: written as JSON only, its fields added by assignment.
  const summary: Record<string, unknown> = blank({
    kind: event.kind,
    // Where the ID came from, never the ID: it is derived from the conversation and turn IDs.
    eventId: event.kind === 'tool_call' ? 'the call' : event.eventId.startsWith('k/') ? 'from turnId, hashed' : 'random: no turnId',
    fields: Object.keys(event).filter((key) => !SDK_FIELDS.has(key)).sort(),
    bytes: entry.bytes,
  })
  if (event.kind === 'turn' && event.toolCalls) summary.toolCalls = event.toolCalls.length
  if (event.kind === 'tool_call' && event.agent?.toolCalls) summary.toolCalls = event.agent.toolCalls.length
  if (event.truncated) summary.truncated = event.truncated
  if (event.dropped) summary.dropped = event.dropped
  if (event.redactions) summary.redactions = event.redactions
  if (Object.keys(entry.ignoredFields).length > 0) summary.ignoredFields = entry.ignoredFields
  if (Object.keys(entry.ignoredToolCallFields).length > 0) summary.ignoredToolCallFields = entry.ignoredToolCallFields
  return `[lsquare dry-run] ${JSON.stringify(summary)}`
}

type Ack = 'accepted' | { rejected: string; resetsAt: string | undefined }

/**
 * Each event counts as sent only if LSquare names it as accepted or duplicate, and not also as
 * rejected. Undefined when the body isn't an acknowledgement at all.
 */
function acknowledgements(text: string): Map<string, Ack | 'unexpected'> | undefined {
  let body: unknown
  try {
    body = JSON.parse(text)
  } catch {
    return undefined
  }
  if (!isPlainObject(body)) return undefined
  const acks = new Map<string, Ack | 'unexpected'>()
  for (const key of ['accepted', 'duplicates']) {
    const list = body[key]
    if (!Array.isArray(list)) continue
    for (const id of list) if (typeof id === 'string') acks.set(id, 'accepted')
  }
  if (Array.isArray(body.rejected)) {
    for (const item of body.rejected as unknown[]) {
      if (!isPlainObject(item) || typeof item.eventId !== 'string') continue
      if (acks.has(item.eventId)) {
        acks.set(item.eventId, 'unexpected')
        continue
      }
      const reason = typeof item.reason === 'string' && REJECTION_REASONS.has(item.reason) ? item.reason : 'unspecified'
      acks.set(item.eventId, { rejected: reason, resetsAt: typeof item.resetsAt === 'string' ? item.resetsAt : undefined })
    }
  }
  return acks
}

/**
 * Reads at most `limit` bytes; undefined when larger or unreadable. A web stream is read through its
 * reader (not every implementation is async-iterable); a Node stream, as some fetch polyfills
 * return, by async iteration. Stopping early cancels or destroys either, and so does `signal`, the
 * request's, aborted at its timeout or once none of its events needs it: a custom fetch that ignores
 * the abort leaves no body read stalled behind.
 */
async function readBody(body: unknown, limit: number, signal: AbortSignal): Promise<string | undefined> {
  if (body === null || body === undefined) return ''
  const chunks: Uint8Array[] = []
  let total = 0
  const encoder = new TextEncoder()
  const keep = (chunk: unknown): boolean => {
    silence(chunk)
    // A string's UTF-8 size is at least its length: refuse an oversized one before copying it.
    if (typeof chunk === 'string' && total + chunk.length > limit) return false
    const bytes = typeof chunk === 'string' ? encoder.encode(chunk) : chunk
    // By its internal slot: `instanceof` would run a Proxy's getPrototypeOf trap.
    if (!isUint8Array(bytes)) return false
    total += bytes.byteLength
    chunks.push(bytes)
    return total <= limit
  }
  quietErrors(body)
  // Each read once: the one checked is the one called. The reader, the iterator and each result
  // are the app's values too: their methods are read once each, as needed, and called by
  // Reflect.apply; each result's `done` and `value` once, silenced; each answer awaited, as
  // `for await` and a reader's caller await one.
  const getReader = member(body, 'getReader')
  const iterate = typeof getReader === 'function' ? undefined : member(body, Symbol.asyncIterator)
  /** The reader or the iterator; its `read` or `next`; and its method that stops it early, `cancel` or `return`, read once, as it stops. */
  let source: unknown
  let step: unknown
  let stopping: string
  if (typeof getReader === 'function') {
    source = Reflect.apply(getReader, body, [])
    silence(source)
    step = member(source, 'read')
    stopping = 'cancel'
  } else if (typeof iterate === 'function') {
    source = Reflect.apply(iterate, body, [])
    silence(source)
    step = member(source, 'next')
    stopping = 'return'
  } else {
    return undefined
  }
  // As calling it would: a reader or iterator without one fails the read, as a network error does.
  if (typeof step !== 'function') throw new TypeError('A body reader has no method to read with.')
  let stopped = false
  const close = () => {
    try {
      if (stopped) return
      stopped = true
      // A reader is cancelled; an iterator closed, as `for await` closes one it leaves early.
      const method = member(source, stopping)
      if (typeof method === 'function') swallow(Reflect.apply(method, source, []))
    } catch {
      // Nothing more to do.
    }
  }
  // Aborted: a reader is cancelled; a Node stream, destroyed (see discard()).
  const off = whenAborted(signal, typeof getReader === 'function' ? close : () => discard(body))
  try {
    for (;;) {
      const result: unknown = await Reflect.apply(step, source, [])
      // As `for await` and a reader's caller require: a result that isn't an object fails the read.
      if ((typeof result !== 'object' && typeof result !== 'function') || result === null) throw new TypeError('A body read gave no result.')
      const done = member(result, 'done')
      if (done) break
      if (!keep(member(result, 'value'))) {
        close()
        return undefined
      }
    }
  } finally {
    off()
  }
  const joined = new Uint8Array(total)
  let offset = 0
  for (const chunk of chunks) {
    joined.set(chunk, offset)
    offset += chunk.byteLength
  }
  return new TextDecoder().decode(joined)
}

/** Stops a body the SDK won't read: a web stream is cancelled, a Node stream destroyed. */
function discard(body: unknown): void {
  try {
    // Each read once, as readBody() reads them: the one checked is the one called.
    const cancel = member(body, 'cancel')
    if (typeof cancel === 'function') {
      swallow(Reflect.apply(cancel, body, []))
      return
    }
    const destroy = member(body, 'destroy')
    if (typeof destroy === 'function') {
      quietErrors(body)
      silence(Reflect.apply(destroy, body, []))
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
  fetching.then(
    (late) => {
      try {
        discard(member(late, 'body'))
      } catch {
        // Nothing more to do.
      }
    },
    () => {},
  )
}

/**
 * Gives a Node stream an 'error' listener. One can emit 'error' after the SDK is done with it (while
 * being destroyed, say); with nobody listening, Node would throw it into the app's process.
 */
function quietErrors(stream: unknown): void {
  try {
    call(stream, 'on', 'error', () => {})
  } catch {
    // Not an event emitter after all.
  }
}

/** A response's header, through its `headers`' `get`, each read once (see member()); null unless a string. */
function header(response: unknown, name: string): string | null {
  const value = call(member(response, 'headers'), 'get', name)
  return typeof value === 'string' ? value : null
}

/**
 * A member of what a custom fetch answered (a response, its headers or body), read once, through
 * its prototype as a class's getters and methods are, and silenced: undefined for what isn't an
 * object. Throws what a getter throws.
 */
function member(value: unknown, key: string | symbol): unknown {
  if ((typeof value !== 'object' && typeof value !== 'function') || value === null) return undefined
  const item = (value as Record<string | symbol, unknown>)[key]
  silence(item)
  return item
}

/** Calls `value`'s method `name`, read once as member() reads it, and returns what it returns, silenced; undefined when it isn't one. Throws what it throws. */
function call(value: unknown, name: string, ...args: unknown[]): unknown {
  const method = member(value, name)
  if (typeof method !== 'function') return undefined
  const result: unknown = Reflect.apply(method, value, args)
  silence(result)
  return result
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

/**
 * Set variables only: for an unset name, process.env falls back to Object.prototype, polluted or
 * not. And the runtime's own `process` and `env`, or their class's, never Object.prototype's, which
 * the global object inherits from: on a runtime without a `process` (Workers on an older
 * compatibility date), a polluted one would give LSQUARE_ENDPOINT or LSQUARE_DRY_RUN. Each value
 * is silenced as it is read, the variable's too: a getter may give a rejected promise, which
 * nothing else would handle.
 */
function env(name: string): string | undefined {
  try {
    const runtime = definedBelowObjectPrototype(globalThis, 'process') ? (globalThis as { process?: unknown }).process : undefined
    silence(runtime)
    if (typeof runtime !== 'object' || runtime === null || !definedBelowObjectPrototype(runtime, 'env')) return undefined
    const vars = (runtime as { env?: unknown }).env
    silence(vars)
    if (typeof vars !== 'object' || vars === null || !hasOwn(vars, name)) return undefined
    const value = (vars as Record<string, unknown>)[name]
    silence(value)
    return typeof value === 'string' ? value : undefined
  } catch {
    // A getter that throws: the variable can't be read, so it is unset.
    return undefined
  }
}

function unref<T>(timer: T): T {
  ;(timer as { unref?: () => void }).unref?.()
  return timer
}
