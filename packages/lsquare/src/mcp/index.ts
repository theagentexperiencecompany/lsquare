// Parts adapted from AgentCat's TypeScript SDK (MIT, © 2025 AgentCat, Inc.; see THIRD_PARTY_NOTICES):
// recognising a server by its shape, hooking the handler map and re-arming when handlers are set
// again, the tap on each tool's own function, reading the client's info, detecting multi-round
// calls, and taking the extras' inputs out of a call.
import type { AsyncLocalStorage } from 'node:async_hooks'
import { createHash, randomUUID } from 'node:crypto'
import { types } from 'node:util'
import type { CallScope, Internal, LSquare } from '../client.js'
import type { CallIds, CallRecord } from '../event.js'
import { blank, definedBelowObjectPrototype, hasOwn } from '../json.js'
import type { ErrorCategory, LSquareError } from '../types.js'
import { advertise, idBlock, INTENT, listedOwnership, MISSING_TOOLS, missingToolsReply, missingToolsTool, newSessionId, type Ownership, SESSION_FORMAT, SESSION_ID, sameOwnership, toolOwnership } from './extras.js'
import { fresh, type Lists, mark, newLists, newWait, type Reader, type Snapshot, waitOnce, withSnapshot } from './lists.js'
import { adopted, copied, declared, follow, own, read, silence } from './read.js'

const { isMap } = types

/** The client's internal API, by key: never `instanceof`, so two copies of the SDK still meet. */
const INTERNAL = Symbol.for('lsquare.client.internal/1')
/** Marks a server already instrumented, by any copy of the SDK. */
const INSTRUMENTED = Symbol.for('lsquare.mcp.instrumented/1')
/** Marks a tool function already tapped. */
const TAPPED = Symbol.for('lsquare.mcp.tapped/1')

const CLIENT_INFO_KEY = 'io.modelcontextprotocol/clientInfo'
const PROTOCOL_VERSION_KEY = 'io.modelcontextprotocol/protocolVersion'
/** How long a call's event waits for `identify` after the tool returns. */
const IDENTIFY_WAIT_MS = 1_000
/** How long `waitUntil` keeps the request alive for a call's event, at most. */
const WAIT_UNTIL_MS = 30_000

/** The IDs `identify` knows for a call, and optionally metadata, with 0.1's rules. */
export interface Identity {
  orgId?: string | null | undefined
  userId?: string | null | undefined
  conversationId?: string | null | undefined
  metadata?: Record<string, unknown> | null | undefined
}

export interface IdentifyInput {
  toolName: string
  /** The arguments as the client sent them, before the MCP SDK checked them. */
  args: unknown
  /** The MCP SDK's request context: auth info, the HTTP request. Only the IDs returned are sent. */
  context: unknown
  /**
   * Fires at the hook's deadline, 1 second after the tool returns (sooner when the reply waits for
   * the event, within a `flushTimeoutMs` above 0), if it is still running then. Once it has
   * answered, it never fires.
   */
  signal: AbortSignal
}

export interface InstrumentOptions {
  /** Who the call is for. Runs alongside the tool, never before it. */
  identify?: ((input: IdentifyInput) => Identity | null | undefined | Promise<Identity | null | undefined>) | undefined
  /** Record arguments: everywhere, nowhere, or for the tools it returns true for. */
  inputs?: boolean | ((toolName: string) => boolean) | undefined
  /** Record results: everywhere, nowhere, or for the tools it returns true for. Strings are cut at 4,000 characters. */
  results?: boolean | ((toolName: string) => boolean) | undefined
  /** The current request's `waitUntil` (Cloudflare Workers, Vercel): each call's sending is handed to it. */
  waitUntil?: ((promise: Promise<unknown>) => void) | undefined
  /** Wait for each call's event to be sent, up to `flushTimeoutMs`, before replying. */
  flushBeforeReply?: boolean | undefined
  /**
   * What the wrapper adds to the server's tools: `session_id` and `intent` inputs, and a
   * `get_more_tools` tool. All on by default on a high-level `McpServer`; each can be turned off,
   * and `false` turns them all off.
   */
  extras?: false | { sessions?: boolean | undefined; intent?: boolean | undefined; missingTools?: boolean | undefined } | undefined
}

type Handler = (request: unknown, extra: unknown) => unknown

interface Extras {
  sessions: boolean
  intent: boolean
  missingTools: boolean
}

const NO_EXTRAS: Extras = { sessions: false, intent: false, missingTools: false }
/** The first protocol version without protocol sessions. */
const SESSIONLESS_PROTOCOL = '2026-07-28'

interface LowLevel {
  setRequestHandler: (...args: unknown[]) => unknown
  _requestHandlers: Map<string, Handler>
  _serverInfo?: unknown
  getClientVersion?: () => unknown
  _negotiatedProtocolVersion?: unknown
  _onclose?: (...args: unknown[]) => unknown
}

interface HighLevel {
  server: LowLevel
  _registeredTools?: Record<string, Record<string, unknown>>
  _toolHandlersInitialized?: unknown
}

/** What a wrapped server keeps, on itself: the wrapper has no state of its own. */
interface Instrumented {
  internal: Internal
  options: Settings
  low: LowLevel
  high: HighLevel | undefined
  /** The server's request handlers, as recognise() found them: read once, never again (see recognise()). */
  handlers: Map<string, Handler>
  /** The app replaced McpServer's tools/call handler: treated as a low-level server. */
  replaced: boolean
  /** The extras while McpServer's own tools/call handler is in place: none on a low-level server. */
  highExtras: Extras
  /** The extras passed as true: what a low-level server, or one whose handler was replaced, gets. */
  lowExtras: Extras
  wrapper: Handler | undefined
  list: Handler | undefined
  /** The tools/list handler the wrapper wraps: the one a low-level server's snapshot is read from. */
  listOriginal: Handler | undefined
  /** A low-level server's snapshot of its own tool list. */
  lists: Lists
  initialize: Handler | undefined
  /** The version the last `initialize` negotiated, which SDK 1.x doesn't keep. */
  protocolVersion: string | undefined
  /** The session made for a stdio connection, which has none of its own: one per transport, which lasts as long as the process. */
  stdio: { transport: unknown; id: string } | undefined
  /** The stdio connection whose stdin the wrapper listens to for its end (see watchStdin()), and what stops that. */
  stdin: { transport: unknown; stop: () => void } | undefined
}

interface Settings {
  identify: InstrumentOptions['identify']
  inputs: (toolName: string) => boolean
  /** The same, asked when listing get_more_tools, with a warning of its own: a function may only know the app's tools. */
  listingInputs: (toolName: string) => boolean
  /** How `inputs` was given, for dry run, which runs outside any request and so doesn't ask a function. */
  inputsMode: 'off' | 'asked' | 'on'
  results: (toolName: string) => boolean
  waitUntil: InstrumentOptions['waitUntil']
  flushBeforeReply: boolean
  /** Each extra on unless passed as false: a high-level server's defaults. */
  extras: Extras
  /** Each extra passed as true: a low-level server's. */
  explicit: Extras
}

/**
 * The client `instrument()` takes: an `LSquare`, from the ESM build or the CommonJS one. Its public
 * methods rather than the class, whose two declarations TypeScript treats as different types, so a
 * client made by the CommonJS build passes to the ESM wrapper and the other way round, as at
 * runtime: the wrapper recognises the client by its shape.
 */
export type LSquareClient = Pick<LSquare, 'capture' | 'currentCallId' | 'flush' | 'shutdown'>

/**
 * Records every `tools/call` of `server`, an MCP TypeScript SDK 1.x or 2.x server, high-level
 * (`McpServer`) or low-level (`Server`), through `client`. Returns the server it was given. Never
 * throws: a server it doesn't recognise, or a disabled client, leaves the server as it is, with a
 * warning. Instrumenting a server twice does nothing the second time. Nothing it does can make a
 * tool call fail.
 */
export function instrument<T>(server: T, client: LSquareClient, options: InstrumentOptions = {}): T {
  try {
    const internal = internalOf(client)
    if (!internal) {
      warn('[lsquare] instrument() needs an LSquare client: the server is left as it is.')
      return server
    }
    if (internal === 'other') {
      warn('[lsquare] instrument() needs a client from the same version of lsquare: the server is left as it is.')
      return server
    }
    if (internal.mode === 'disabled') {
      internal.warnOnce('instrument-disabled', '[lsquare] No apiKey: instrument() leaves servers as they are.')
      return server
    }
    const found = recognise(server)
    if (!found) {
      internal.warnOnce('instrument-unknown', "[lsquare] instrument() doesn't recognise this server: it is left as it is.")
      return server
    }
    const { low, high, handlers, setRequestHandler } = found
    if (instrumented(low)) return server
    // Tracking calls needs AsyncLocalStorage: on from here, for this client. If it can't start,
    // the server is left as it is.
    internal.calls()

    const settings = readOptions(options, internal)
    const state: Instrumented = {
      internal,
      options: settings,
      low,
      high,
      handlers,
      replaced: false,
      // A low-level server's tool definitions exist only in its own tools/list handler.
      highExtras: high ? settings.extras : NO_EXTRAS,
      lowExtras: settings.explicit,
      wrapper: undefined,
      list: undefined,
      listOriginal: undefined,
      lists: newLists(internal.clock),
      initialize: undefined,
      protocolVersion: undefined,
      stdio: undefined,
      stdin: undefined,
    }
    // A descriptor without a prototype: a polluted `get` or `writable` can't be read into it.
    Object.defineProperty(low, INSTRUMENTED, blank({ value: state, enumerable: false }))

    // Re-arm whenever a handler is set: McpServer sets tools/call when its first tool is
    // registered, and an app may replace it. All arguments are passed on as given, to the method
    // recognise() read and checked, never read again.
    low.setRequestHandler = function (this: unknown, ...args: unknown[]) {
      const before = handlerOf(handlers, 'tools/call')
      const initialized = declared(high, '_toolHandlersInitialized') === true
      const result = Reflect.apply(setRequestHandler, this, args)
      noteReplacement(state, before, initialized)
      arm(state)
      return result
    }
    arm(state)
    if (anyExtra(state.lowExtras)) watchListChanged(state)
    flushOnClose(state)
    watchConnect(state)
    // Instrumented once connected: listened to from now.
    watchStdin(state)
    warnServerless(settings, internal)
    if (internal.mode === 'dry-run') internal.log('info', `[lsquare dry-run] ${JSON.stringify(dryRun(state))}`)
  } catch {
    // Instrumenting must never break the app: whatever was installed passes calls through.
  }
  return server
}

/**
 * Whether a copy of the SDK instrumented the server already: its mark, read as any of the app's
 * values is (see read()), is the state that copy keeps there, an object holding its client's
 * internals. A Proxy server's get trap may answer anything for it, a rejected promise included,
 * which is no mark.
 */
function instrumented(low: LowLevel): boolean {
  const mark = read(low, INSTRUMENTED)
  if (typeof mark !== 'object' || mark === null || types.isPromise(mark)) return false
  const internal = read(mark, 'internal')
  return typeof internal === 'object' && internal !== null
}

/** What the wrapper calls on the client. A client from another copy of lsquare may lack some. */
const NEEDED = ['record', 'calls', 'callback', 'settled', 'track', 'delivered', 'flush', 'log', 'warnOnce', 'report', 'validId', 'turnId', 'turnKey'] as const

/** The client's internals; 'other' for a client from another version, which lacks some of them; undefined if it isn't one. */
function internalOf(client: unknown): Internal | 'other' | undefined {
  try {
    // Read as any of the app's values is (see read()): what it was handed may be anything.
    const internal = read(client, INTERNAL) as Internal | undefined
    if (!internal || typeof internal.record !== 'function') return undefined
    const complete =
      NEEDED.every((name) => typeof internal[name] === 'function') &&
      typeof internal.clock?.now === 'function' &&
      typeof internal.identifying === 'object' &&
      internal.identifying !== null &&
      typeof internal.identified === 'object' &&
      internal.identified !== null
    return complete ? internal : 'other'
  } catch {
    return undefined
  }
}

/** A warning with no client to log it: to the runtime's console, read as the client reads the runtime's values (see declared()), its answer silenced. */
function warn(message: string): void {
  try {
    const target = declared(globalThis, 'console')
    const method = declared(target, 'warn')
    if (typeof method === 'function') silence(Reflect.apply(method, target, [message]))
  } catch {
    // Nowhere to say it.
  }
}

/**
 * A server by its shape, importing nothing from the MCP SDKs: a request handler map and
 * `setRequestHandler`, on the object itself (a low-level `Server`) or on its `server` (an
 * `McpServer`, which also registers tools). The map and the method are read once, here, and what
 * was checked is what the wrapper uses: a getter read again could give something else. The map is
 * told by its internal slot (`isMap`): `instanceof` would run a Proxy's getPrototypeOf trap, whose
 * answer JavaScript consumes, and nothing else could handle a promise it gave.
 */
function recognise(server: unknown): { low: LowLevel; high: HighLevel | undefined; handlers: Map<string, Handler>; setRequestHandler: (...args: unknown[]) => unknown } | undefined {
  if (typeof server !== 'object' || server === null) return undefined
  // Its own fields and its class's methods, never Object.prototype's (see declared()).
  const inner = declared(server, 'server')
  const highLevel = typeof inner === 'object' && inner !== null && (typeof declared(server, 'registerTool') === 'function' || typeof declared(server, 'tool') === 'function')
  const low = highLevel ? inner : server
  const setRequestHandler = declared(low, 'setRequestHandler')
  const handlers = declared(low, '_requestHandlers')
  if (typeof setRequestHandler !== 'function' || !isMap(handlers)) return undefined
  return {
    low: low as LowLevel,
    high: highLevel ? (server as HighLevel) : undefined,
    handlers: handlers as Map<string, Handler>,
    setRequestHandler: setRequestHandler as (...args: unknown[]) => unknown,
  }
}

function handlerOf(handlers: Map<string, Handler>, method: string): Handler | undefined {
  try {
    return handlers.get(method)
  } catch {
    return undefined
  }
}

/**
 * McpServer sets its tools/call handler once, then marks its handlers initialized. A tools/call
 * handler set after that is the app's own: the server is then treated as a low-level one.
 */
function noteReplacement(state: Instrumented, before: Handler | undefined, initialized: boolean): void {
  try {
    const after = handlerOf(state.handlers, 'tools/call')
    if (state.high && initialized && after !== before && after !== state.wrapper) state.replaced = true
  } catch {
    // Unknown: left as it was.
  }
}

function readOptions(options: InstrumentOptions, internal: Internal): Settings {
  // As the client reads its own options: never from a polluted Object.prototype (see declared()).
  const option = (key: keyof InstrumentOptions): unknown => declared(options, key)
  // A function that throws, or answers with a promise (which can't be waited for), records nothing
  // for that call: the field stays out of its event, and one warning per option says so.
  const unanswered = (key: 'inputs' | 'results') => `[lsquare] The ${key} function threw or returned a promise: ${key} aren't recorded for the calls it answered that way.`
  const choice = (key: 'inputs' | 'results', value: unknown, warnKey = `${key}-unanswered`, warning = unanswered(key)) => (toolName: string) => {
    if (typeof value !== 'function') return value !== false
    try {
      const answer = (value as (name: string) => unknown)(toolName)
      // Silenced first: a promise whose `then` getter throws is still never left unhandled.
      silence(answer)
      if ((typeof answer !== 'object' && typeof answer !== 'function') || answer === null) return answer !== false
      // Read once, in here: a `then` getter that throws records nothing either, and a promise it
      // gives is silenced. Any other object isn't `false`, so it records them.
      const then = (answer as { then?: unknown }).then
      silence(then)
      if (typeof then !== 'function') return true
    } catch {
      // Below.
    }
    internal.warnOnce(warnKey, warning)
    return false
  }
  const inputs = option('inputs')
  const identify = option('identify')
  const waitUntil = option('waitUntil')
  const flushBeforeReply = option('flushBeforeReply') === true
  if (typeof waitUntil === 'function' && flushBeforeReply) {
    internal.warnOnce('flush-ignored', '[lsquare] Both waitUntil and flushBeforeReply are set: waitUntil is used, and flushBeforeReply ignored.')
  }
  // Each extra left out keeps its level's default: on for a high-level server, off for a low-level
  // one. `false` turns them all off. Any other value that isn't a plain object, an array or a
  // function included, is ignored, with a warning, and none of its fields is read.
  const extras = option('extras')
  const off = extras === false
  const ignored = !off && extras != null && (typeof extras !== 'object' || listLike(extras))
  if (ignored) internal.warnOnce('extras-ignored', '[lsquare] extras is neither false nor an object: it is ignored, and each extra keeps its default.')
  const given = off || ignored ? undefined : extras
  const extra = (key: keyof Extras) => !off && declared(given, key) !== false
  const explicit = (key: keyof Extras) => declared(given, key) === true
  return {
    identify: typeof identify === 'function' ? (identify as Settings['identify']) : undefined,
    inputs: choice('inputs', inputs),
    listingInputs: choice('inputs', inputs, `inputs-unanswered/${MISSING_TOOLS}`, `[lsquare] The inputs function threw or returned a promise when asked about ${MISSING_TOOLS}: it isn't listed.`),
    inputsMode: inputs === false ? 'off' : typeof inputs === 'function' ? 'asked' : 'on',
    results: choice('results', option('results')),
    waitUntil: typeof waitUntil === 'function' ? (waitUntil as Settings['waitUntil']) : undefined,
    flushBeforeReply: flushBeforeReply && typeof waitUntil !== 'function',
    extras: { sessions: extra('sessions'), intent: extra('intent'), missingTools: extra('missingTools') },
    explicit: { sessions: explicit('sessions'), intent: explicit('intent'), missingTools: explicit('missingTools') },
  }
}

/** An array, or a value that can't say (a revoked proxy): never an options object. */
function listLike(value: unknown): boolean {
  try {
    return Array.isArray(value)
  } catch {
    return true
  }
}

/**
 * Puts the wrapper in front of the current `tools/call` handler, unless it already is, and of
 * `tools/list` on a server with extras, and watches `initialize` for the protocol version it
 * negotiates.
 */
function arm(state: Instrumented): void {
  try {
    const handlers = state.handlers
    const current = handlers.get('tools/call')
    if (current !== undefined && current !== state.wrapper) {
      const original = current
      const wrapper: Handler = (request, extra) => onCall(state, original, request, extra)
      state.wrapper = wrapper
      handlers.set('tools/call', wrapper)
    }
    const list = handlers.get('tools/list')
    if ((anyExtra(state.highExtras) || anyExtra(state.lowExtras)) && list !== undefined && list !== state.list) {
      const original = list
      // Another tools/list handler: a snapshot read from the one before is out of date.
      if (state.listOriginal !== undefined) mark(state.lists, false)
      state.listOriginal = original
      const wrapper: Handler = (request, extra) => onList(state, original, request, extra)
      state.list = wrapper
      handlers.set('tools/list', wrapper)
    }
    const init = handlers.get('initialize')
    if (init !== undefined && init !== state.initialize) {
      const original = init
      const wrapper: Handler = (request, extra) => {
        const out = original(request, extra)
        // Its own, as it goes to the client.
        const note = (result: unknown) => {
          const version = own(result, 'protocolVersion')
          if (typeof version === 'string') state.protocolVersion = version
        }
        // A thenable is adopted once, by the wrapper, as the SDK's dispatch adopts a handler's
        // answer, by a resolve function (see adopted()), and the SDK given its value: its `then` is
        // run once, never once for each.
        const adopting = adopted(out, true)
        if (adopting === undefined) {
          try {
            note(out)
          } catch {
            // Unknown version: left out of events.
          }
          return out
        }
        return adopting.then((value) => {
          try {
            note(value)
          } catch {
            // Unknown version: left out of events.
          }
          return value
        })
      }
      state.initialize = wrapper
      handlers.set('initialize', wrapper)
    }
  } catch {
    // Left as it was: calls pass through unrecorded.
  }
}

/**
 * Marks the snapshot out of date whenever the server sends notifications/tools/list_changed, before
 * sending it on as it came. SDK 2.x sends every notification through _notificationViaCodec, a
 * handler's own (ctx.mcpReq.notify) included; SDK 1.x through notification(). Only one is watched,
 * so a notification marks once.
 */
function watchListChanged(state: Instrumented): void {
  try {
    const low = state.low as unknown as Record<string, unknown>
    // The server's own methods, its class's: never a polluted Object.prototype's (see declared()).
    // Each read once: the one checked is the one wrapped.
    const viaCodec = declared(low, '_notificationViaCodec')
    const key = typeof viaCodec === 'function' ? '_notificationViaCodec' : 'notification'
    const send = key === '_notificationViaCodec' ? viaCodec : declared(low, 'notification')
    if (typeof send !== 'function') return
    const position = key === '_notificationViaCodec' ? 1 : 0
    low[key] = function (this: unknown, ...args: unknown[]) {
      try {
        if (own(args[position], 'method') === 'notifications/tools/list_changed') mark(state.lists, true)
      } catch {
        // Unknown: the snapshot stays as it is.
      }
      return Reflect.apply(send, this, args)
    }
  } catch {
    // Left as it was: the snapshot is still read again when a client's listing disagrees with it.
  }
}

/**
 * When a stdio connection ends, what's queued is flushed, once: when the server closes, or when its
 * stdin ends, which is how a stdio client shuts it, and which SDK 1.x's transport never hears (see
 * watchStdin()). A server on any other transport doesn't flush when it closes: a per-request one
 * closes after every request, and a long-lived process sends in the background anyway.
 */
function flushOnClose(state: Instrumented): void {
  const close = declared(state.low, '_onclose') as ((...args: unknown[]) => unknown) | undefined
  if (typeof close !== 'function') return
  state.low._onclose = function (this: unknown, ...args: unknown[]) {
    // Read first: both SDKs let go of the transport before their close returns.
    const transport = declared(state.low, 'transport')
    try {
      return close.apply(this, args)
    } finally {
      closeFlush(state, transport)
    }
  }
}

/** Connections whose close flush has started, by their transport: their close and their stdin's end start it once between them. */
const closing = new WeakSet<object>()

/** Starts the close flush of `transport`'s connection, if it is stdio and its flush hasn't started. Never throws. */
function closeFlush(state: Instrumented, transport: unknown): void {
  try {
    const watch = state.stdin
    if (watch !== undefined && watch.transport === transport) watch.stop()
    if (stdioOf(transport) === undefined || closing.has(transport as object)) return
    closing.add(transport as object)
    flushQueued(state)
  } catch {
    // The close itself is unaffected.
  }
}

/** A flush of what's queued, within flushTimeoutMs, in the background: its wait keeps a Node process alive until it ends. Never throws. */
function flushQueued(state: Instrumented): void {
  try {
    void Promise.resolve(state.internal.flush(state.internal.flushTimeoutMs)).catch(() => {})
  } catch {
    // Nothing is waited for: sending carries on in the background.
  }
}

/**
 * Where a transport recognised as stdio keeps its streams: the transport itself (either SDK's
 * StdioServerTransport, with its `_stdin` and `_stdout`), or, for the channel SDK 2.x's
 * serveStdio() connects each instance to, the transport it writes through (its `_wire`).
 * Undefined for any other transport. All private fields. A release that renames `_wire` costs more
 * than grouping (each of that channel's calls then gets an ID of its own): the channel's close then
 * starts no flush either, so what is still queued at exit, a retry included, can be lost.
 */
function stdioOf(transport: unknown): unknown {
  for (const candidate of [transport, declared(transport, '_wire')]) {
    const stream = (key: string) => {
      const value = declared(candidate, key)
      return typeof value === 'object' && value !== null
    }
    if (stream('_stdin') && stream('_stdout')) return candidate
  }
  return undefined
}

/**
 * Listens to the stdin of each stdio connection the server makes from here (see watchStdin()):
 * both SDKs' connect() has put its transport in place by the time it returns its promise.
 */
function watchConnect(state: Instrumented): void {
  try {
    const low = state.low as unknown as Record<string, unknown>
    const connect = declared(low, 'connect')
    if (typeof connect !== 'function') return
    low.connect = function (this: unknown, ...args: unknown[]) {
      const connecting = (connect as (...args: unknown[]) => unknown).apply(this, args)
      watchStdin(state)
      return connecting
    }
  } catch {
    // Unheard: the server's own close still flushes.
  }
}

/**
 * A stdio client shuts the server by closing its stdin, then waits for it to exit. SDK 1.x's
 * transport doesn't listen for that end, so its server never closes, and Node exits once nothing
 * is left to run, an event waiting for its retry with it. So, from the moment a stdio connection
 * starts until the server closes, the wrapper listens for its stdin's end itself: whichever of that
 * and the server's close comes first starts the flush (SDK 2.x's transport hears the end, and
 * closes the server). Never throws.
 */
function watchStdin(state: Instrumented): void {
  try {
    const transport = declared(state.low, 'transport')
    if (transport === undefined || state.stdin?.transport === transport) return
    const stdin = declared(stdioOf(transport), '_stdin')
    const on = declared(stdin, 'on')
    const off = declared(stdin, 'off') ?? declared(stdin, 'removeListener')
    if (typeof on !== 'function' || typeof off !== 'function') return
    state.stdin?.stop()
    const ended = () => closeFlush(state, transport)
    // What they return is silenced: a stream's own methods may be the app's (see read()).
    const watch: NonNullable<Instrumented['stdin']> = {
      transport,
      stop: () => {
        if (state.stdin === watch) state.stdin = undefined
        try {
          silence(Reflect.apply(off, stdin, ['end', ended]))
          silence(Reflect.apply(off, stdin, ['close', ended]))
        } catch {
          // Then it fires once more, and finds the flush started.
        }
      },
    }
    state.stdin = watch
    silence(Reflect.apply(on, stdin, ['end', ended]))
    silence(Reflect.apply(on, stdin, ['close', ended]))
  } catch {
    // Unheard: the server's own close still flushes.
  }
}

/** Hosts known to stop work after the reply, when neither option says how to keep sending. */
function warnServerless(settings: Settings, internal: Internal): void {
  if (settings.waitUntil || settings.flushBeforeReply) return
  const host = serverlessHost()
  if (!host) return
  internal.warnOnce(
    'serverless',
    `[lsquare] This looks like ${host}, which can stop work once a reply is sent: pass waitUntil, or flushBeforeReply: true, to instrument(), or events may be lost.`,
  )
}

function serverlessHost(): string | undefined {
  try {
    // Each variable the environment's own, as the client reads its own: process.env, and the global
    // object, inherit from Object.prototype, which a polluted one mustn't answer for.
    const vars = own(declared(globalThis, 'process'), 'env')
    const env = (name: string) => own(vars, name)
    if (env('AWS_LAMBDA_FUNCTION_NAME')) return 'AWS Lambda'
    if (env('K_SERVICE')) return 'Cloud Run (unless its CPU is always allocated)'
    if (env('VERCEL')) return 'Vercel'
    if (declared(declared(globalThis, 'navigator'), 'userAgent') === 'Cloudflare-Workers') {
      return 'Cloudflare Workers (unless the server runs in a Durable Object)'
    }
  } catch {
    // An odd environment: no warning.
  }
  return undefined
}

function anyExtra(extras: Extras): boolean {
  return extras.sessions || extras.intent || extras.missingTools
}

/** McpServer's own tools/call handler is in place, so the tools' definitions decide. */
function highMode(state: Instrumented): boolean {
  return state.high !== undefined && !state.replaced
}

/** A high-level server's extras apply now. */
function highOn(state: Instrumented): boolean {
  return highMode(state) && anyExtra(state.highExtras)
}

/** A low-level server's extras apply now, or those of a server whose handler was replaced: the snapshot decides. */
function lowOn(state: Instrumented): boolean {
  return !highMode(state) && anyExtra(state.lowExtras)
}

/** How a low-level server's snapshot is read: from its own tools/list handler, as a callback. */
function readerOf(state: Instrumented): Reader {
  return {
    // The map's entry, unless it is the wrapper: then what the wrapper wraps.
    handler: () => {
      const current = handlerOf(state.handlers, 'tools/list')
      return current === state.list ? state.listOriginal : current
    },
    callback: (run) => state.internal.callback(run),
  }
}

/** A tool the extras leave alone, or one with an input of its own of their names, is logged once per client. */
function note(state: Instrumented, extras: Extras, toolName: string, owner: Ownership): void {
  const { internal } = state
  const label = JSON.stringify(toolName)
  if (!owner.readable && (extras.sessions || extras.intent)) {
    internal.warnOnce(`extras-unreadable/${toolName}`, `[lsquare] Tool ${label} gets no session_id or intent: its input schema can't be read safely (${owner.why}), so any input could be its own.`)
  }
  for (const [on, name] of [[extras.sessions, SESSION_ID], [extras.intent, INTENT]] as const) {
    if (on && owner.readable && owner.declared.has(name)) internal.warnOnce(`extras-own/${name}/${toolName}`, `[lsquare] Tool ${label} has its own ${name} input: it is left as it is.`)
  }
}

/**
 * What a registered tool's definition, as the current instance holds it now, says of the extras'
 * inputs; undefined for a name the server hasn't registered.
 */
function ownershipOf(state: Instrumented, toolName: string): Ownership | undefined {
  const tool = own(declared(state.high, '_registeredTools'), toolName)
  if (typeof tool !== 'object' || tool === null) return undefined
  const owner = toolOwnership(tool)
  note(state, state.highExtras, toolName, owner)
  return owner
}

/**
 * Whether to list get_more_tools, once all else says yes: only where its `need` would be recorded,
 * since what it tells agents isn't true otherwise.
 */
function recordsNeed(state: Instrumented): boolean {
  if (state.options.listingInputs(MISSING_TOOLS)) return true
  state.internal.warnOnce(`extras-no-inputs/${MISSING_TOOLS}`, `[lsquare] ${MISSING_TOOLS} isn't offered: inputs are off for it, so what an agent asks for wouldn't be recorded.`)
  return false
}

/** get_more_tools is offered while the server has an enabled tool of its own, and none of that name. */
function offersMissingTools(state: Instrumented): boolean {
  const tools = declared(state.high, '_registeredTools')
  if (typeof tools !== 'object' || tools === null) return false
  if (Object.prototype.hasOwnProperty.call(tools, MISSING_TOOLS)) {
    ownMissingTools(state)
    return false
  }
  return Object.keys(tools).some((name) => Boolean(declared(own(tools, name), 'enabled')))
}

function ownMissingTools(state: Instrumented): void {
  state.internal.warnOnce('extras-own/get_more_tools', `[lsquare] The server has its own ${MISSING_TOOLS} tool: it is left as it is, and LSquare's isn't added.`)
}

/**
 * The extras added to a copy of the tool list, never to the developer's definitions. On a
 * low-level server the page waits for the snapshot, at most a second. Any failure gives the list
 * as it came.
 */
function onList(state: Instrumented, original: Handler, request: unknown, extra: unknown): unknown {
  const listed = original(request, extra)
  try {
    // A thenable is adopted once, by the wrapper, as the SDK's dispatch adopts a handler's answer,
    // by a resolve function (see adopted()), and the SDK given what it lists: an own `then` of a
    // native promise runs, in a job, as the dispatch runs it, never at once by the wrapper.
    if (highOn(state)) {
      const adopting = adopted(listed, true)
      return adopting ? adopting.then((value) => withExtras(state, value)) : withExtras(state, listed)
    }
    if (!lowOn(state)) return listed
    const first = read(read(request, 'params'), 'cursor') === undefined
    const extended = (page: unknown) => withSnapshot(state.lists, readerOf(state), extra, (snapshot) => withLowExtras(state, page, snapshot, first)).catch(() => page)
    const adopting = adopted(listed, true)
    return adopting ? adopting.then(extended) : extended(listed)
  } catch {
    return listed
  }
}

function withExtras(state: Instrumented, listed: unknown): unknown {
  try {
    // A listing that resolves later is checked again: the handler may have been replaced meanwhile.
    if (!highOn(state)) return listed
    // The listing's own fields, as they go to the client (its entries as JSON writes them).
    const tools = listEntries(own(listed, 'tools'))
    if (!tools) return listed
    const extras = state.highExtras
    let changed = false
    let named = false
    const shown = tools.map((tool) => {
      // Each tool's name read once: the one its ownership is looked up by is the one listed.
      const name = own(tool, 'name')
      if (name === MISSING_TOOLS) named = true
      const copy = typeof name === 'string' ? advertised(state, tool, name) : tool
      if (copy !== tool) changed = true
      return copy
    })
    // Added to the last page only, and never next to a tool of that name.
    if (extras.missingTools && own(listed, 'nextCursor') == null && offersMissingTools(state) && !named && recordsNeed(state)) {
      shown.push(missingToolsTool(extras.sessions))
      changed = true
    }
    return changed ? copied(listed as object, { tools: shown }) : listed
  } catch {
    return listed
  }
}

/** A listed tool, named `name`, with the extras' inputs its definition leaves to the wrapper, where its listed schema can take them. */
function advertised(state: Instrumented, tool: unknown, name: string): unknown {
  const owner = ownershipOf(state, name)
  if (!owner?.readable) return tool
  return withInputs(state.highExtras, tool, owner, name)
}

/**
 * The tool with the extras' inputs it doesn't declare, where its listed schema can take them; as
 * it is otherwise. A copy as JSON writes it (see copied()): the name and schema already read are
 * the ones it keeps, never read again.
 */
function withInputs(extras: Extras, tool: unknown, owner: Ownership & { readable: true }, name: string): unknown {
  const names: string[] = []
  if (extras.sessions && !owner.declared.has(SESSION_ID)) names.push(SESSION_ID)
  if (extras.intent && !owner.declared.has(INTENT)) names.push(INTENT)
  const schema = advertise(own(tool, 'inputSchema'), names)
  return schema ? copied(tool as object, { inputSchema: schema }, { name }) : tool
}

/**
 * A list's entries as JSON writes them, its length read once, each entry through its prototype too
 * (holes), every value silenced; undefined for what isn't an array, or one longer than a listing
 * can be. Never its methods (`map`, its iterator), which no JSON reads: a Proxy's answer for one
 * would be the wrapper's alone.
 */
function listEntries(list: unknown, max = MAX_LISTED): unknown[] | undefined {
  if (!Array.isArray(list)) return undefined
  const length = read(list, 'length')
  if (typeof length !== 'number' || !(length <= max)) return undefined
  const entries: unknown[] = []
  for (let index = 0; index < length; index++) entries.push(read(list, String(index)))
  return entries
}

/** The most tools a page the wrapper changes may list, as a low-level server's snapshot reads at most. */
const MAX_LISTED = 10_000

/**
 * A low-level server's page with the extras, where the snapshot and the page agree: a tool gets
 * an input only when both show it doesn't declare it. A page that disagrees, by a tool missing
 * from the snapshot, another reading of a schema, or a list in one page with other names, marks
 * the snapshot out of date, and gets neither inputs on those tools nor get_more_tools.
 */
function withLowExtras(state: Instrumented, page: unknown, snapshot: Snapshot | undefined, first: boolean): unknown {
  try {
    if (!lowOn(state) || !snapshot) return page
    // As in withExtras().
    const tools = listEntries(own(page, 'tools'))
    if (!tools) return page
    const extras = state.lowExtras
    const names = new Set<string>()
    let changed = false
    let disagrees = false
    const shown = tools.map((tool) => {
      const name = own(tool, 'name')
      if (typeof name !== 'string' || names.has(name)) {
        disagrees = true
        return tool
      }
      names.add(name)
      const known = snapshot.tools.get(name)
      const listed = listedOwnership(own(tool, 'inputSchema'))
      if (!known || !sameOwnership(known, listed)) {
        disagrees = true
        return tool
      }
      note(state, extras, name, listed)
      if (!listed.readable) return tool
      const copy = withInputs(extras, tool, listed, name)
      if (copy !== tool) changed = true
      return copy
    })
    const last = own(page, 'nextCursor') == null
    if (first && last && (names.size !== snapshot.tools.size || [...snapshot.tools.keys()].some((name) => !names.has(name)))) disagrees = true
    if (disagrees) mark(state.lists, false)
    if (snapshot.ownMissingTools && extras.missingTools) ownMissingTools(state)
    if (extras.missingTools && last && !disagrees && snapshot.tools.size > 0 && !snapshot.ownMissingTools && !names.has(MISSING_TOOLS) && recordsNeed(state)) {
      shown.push(missingToolsTool(extras.sessions))
      changed = true
    }
    return changed ? copied(page as object, { tools: shown }) : page
  } catch {
    return page
  }
}

/** What the extras take out of a call before the SDK checks it, and whether the wrapper answers it. */
interface Prepared {
  /** The request passed on: a copy without the extras' inputs, or the one that came. */
  request: unknown
  /** Sessions apply to this call: the extra is on, at the server's level now. */
  grouping: boolean
  /** The tool leaves `session_id` to the wrapper. */
  sessions: boolean
  /** A session ID in the right format, sent by the agent. */
  sent: string | undefined
  intent: string | undefined
  /** A call to the wrapper's own get_more_tools. */
  answer: boolean
  /** The handler is the tool (a low-level server): its result is the tool's own. */
  own: boolean
  /** What the `inputs` option answered, if it was asked here: a call asks it once, so the wrapper never answers without recording the need. */
  inputs: boolean | undefined
}

/**
 * Takes the extras' inputs out of a call to a tool whose current definition leaves them to the
 * wrapper, valid or not, into a copy of the request: the request itself is never changed. A tool
 * that declares one of them, or whose schema can't be read, keeps its arguments as sent.
 */
function prepare(state: Instrumented, request: unknown): Prepared {
  const none: Prepared = { request, grouping: false, sessions: false, sent: undefined, intent: undefined, answer: false, own: false, inputs: undefined }
  try {
    if (!highOn(state)) return none
    const extras = state.highExtras
    // Even a tool the extras leave alone is grouped by its protocol session.
    none.grouping = extras.sessions
    const toolName = read(read(request, 'params'), 'name')
    if (typeof toolName !== 'string') return none
    if (extras.missingTools && toolName === MISSING_TOOLS && offersMissingTools(state)) {
      none.inputs = state.options.inputs(toolName)
      if (none.inputs) return strip(none, extras, undefined)
    }
    const owner = ownershipOf(state, toolName)
    return owner?.readable ? strip(none, extras, owner) : none
  } catch {
    return none
  }
}

/**
 * The same for a low-level server, or one whose handler was replaced, from its snapshot, or
 * without one: the call is passed on as it came. Called in the step that calls the handler.
 */
function prepareLow(state: Instrumented, request: unknown, snapshot: Snapshot | undefined): Prepared {
  const extras = state.lowExtras
  const none: Prepared = { request, grouping: extras.sessions, sessions: false, sent: undefined, intent: undefined, answer: false, own: true, inputs: undefined }
  try {
    if (!lowOn(state) || !snapshot) return none
    const toolName = read(read(request, 'params'), 'name')
    if (typeof toolName !== 'string') return none
    if (snapshot.ownMissingTools && extras.missingTools) ownMissingTools(state)
    if (extras.missingTools && toolName === MISSING_TOOLS && snapshot.tools.size > 0 && !snapshot.ownMissingTools) {
      none.inputs = state.options.inputs(toolName)
      if (none.inputs) return strip(none, extras, undefined)
    }
    const owner = snapshot.tools.get(toolName)
    if (owner) note(state, extras, toolName, owner)
    return owner?.readable ? strip(none, extras, owner) : none
  } catch {
    return none
  }
}

/**
 * Takes out the inputs `owner` leaves to the wrapper, or, for get_more_tools (no owner), its
 * session_id, into a copy of the request. Throws on a request that can't be read: the caller then
 * passes the call on as it came.
 */
function strip(none: Prepared, extras: Extras, owner: (Ownership & { readable: true }) | undefined): Prepared {
  const answer = owner === undefined
  const sessions = extras.sessions && !owner?.declared.has(SESSION_ID)
  const intents = !answer && extras.intent && !owner.declared.has(INTENT)
  const prepared: Prepared = { ...none, sessions, answer }
  const request = none.request
  const params = read(request, 'params')
  // The client's own: both SDKs' JSON-RPC parse leaves out a polluted Object.prototype's.
  const args = own(params, 'arguments')
  if (typeof args !== 'object' || args === null || Array.isArray(args)) return prepared
  const has = (key: string) => Object.prototype.hasOwnProperty.call(args, key)
  const removed: string[] = []
  if (sessions && has(SESSION_ID)) {
    removed.push(SESSION_ID)
    const id = read(args, SESSION_ID)
    if (typeof id === 'string' && SESSION_FORMAT.test(id)) prepared.sent = id
  }
  if (intents && has(INTENT)) {
    removed.push(INTENT)
    const intent = read(args, INTENT)
    if (typeof intent === 'string' && intent !== '') prepared.intent = intent
  }
  if (removed.length > 0) {
    const copy: Record<string, unknown> = { ...(args as Record<string, unknown>) }
    for (const key of removed) delete copy[key]
    prepared.request = { ...(request as Record<string, unknown>), params: { ...(params as Record<string, unknown>), arguments: copy } }
  }
  return prepared
}

/**
 * The call's session, decided when the tool returns: the ID the agent sent; else none when the
 * tool called capture() with a conversationId, which groups it; else, for a client on a protocol
 * version with sessions, the transport's; else a new one, given to the agent in a text block at
 * the end of a copy of the tool's own final result. Returns the reply. A tool the extras leave
 * alone never gets a new one.
 */
function group(call: Running, outcome: Outcome, result: unknown): unknown {
  const { extras, scope } = call
  if (!extras.grouping) return result
  // The transport's own ID, sent back, is still that session: it never leaves as it is.
  if (extras.sent !== undefined && extras.sent !== call.protocolSession) {
    call.session = { id: extras.sent, source: 'sent' }
    return result
  }
  const conversationId = scope.agent?.conversationId
  if (typeof conversationId === 'string' && conversationId !== '') return result
  if (call.protocolSession !== undefined) {
    call.session = { id: sessionHash(call.protocolSession), source: 'protocol' }
    return result
  }
  if (!extras.sessions) return result
  // Only the tool's own final result: not a thrown error, a result the SDK made, a task or a round.
  if (outcome.task || outcome.round === 'input_required') return result
  if (outcome.status === 'error' && outcome.error?.category !== 'tool_error') return result
  if (!extras.answer && !extras.own && scope.toolResult === undefined) return result
  try {
    // Its own, as it goes to the client: the copy keeps only those, as JSON writes them, the
    // content read once (see copied() and listEntries()).
    const content = listEntries(own(result, 'content'), Number.MAX_SAFE_INTEGER)
    if (!content) return result
    const id = newSessionId()
    const reply = copied(result as object, { content: [...content, idBlock(id)] })
    call.session = { id, source: 'issued' }
    return reply
  } catch {
    // A result that can't be copied goes as it is, without an ID.
    return result
  }
}

/** A protocol session's ID as events carry it: the ID itself can be a live handle to the session, and grouping only needs the same value each time. */
function sessionHash(id: string): string {
  return createHash('sha256').update(id).digest('hex')
}

/**
 * The session the transport established: a stateful HTTP server's, which it issued itself, or, on
 * stdio (see stdioOf()), one made for the connection, which lasts as long as the process. None
 * from 2026-07-28 on, which has no sessions; a version that isn't known counts as older.
 * `transport` is the instance's as the call came (see onCall()), the connection, behind
 * serveStdio() too (its channel).
 */
function protocolSession(state: Instrumented, version: unknown, extra: unknown, transport: unknown): string | undefined {
  if (typeof version === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(version) && version >= SESSIONLESS_PROTOCOL) return undefined
  // First: neither SDK's stdio transport has a session ID, so the context's is a polluted prototype's.
  if (stdioOf(transport) !== undefined) {
    if (state.stdio?.transport !== transport) state.stdio = { transport, id: randomUUID() }
    return state.stdio?.id
  }
  // Both SDKs give the transport's own session ID (`capturedTransport?.sessionId`), never a header
  // it didn't issue, read through the prototype: a transport without one (SDK 1.x's stateless HTTP
  // transport) gives a polluted Object.prototype's. A string can't show where it came from, and
  // comparing it with Object.prototype's can't either (a getter there answers anew each time it is
  // read), so while Object.prototype has one at all, the context's is the transport's only where
  // the transport declares it too.
  const id = declared(extra, 'sessionId')
  if (typeof id !== 'string' || id === '' || !state.internal.validId(id)) return undefined
  return !hasOwn(Object.prototype, 'sessionId') || declared(transport, 'sessionId') === id ? id : undefined
}

/** What dry run says of a server: what it records, and what the extras can change. */
function dryRun(state: Instrumented): Record<string, unknown> {
  // No prototype, as what it describes: written as JSON only, its fields added by assignment.
  const line: Record<string, unknown> = blank({ server: serverName(state.low) ?? 'unnamed', level: state.high ? 'high' : 'low', records: 'every tools/call' })
  if (!state.high) {
    const extras = state.lowExtras
    if (!anyExtra(extras)) {
      line.extras =
        'off on a low-level server; extras: { sessions: true, intent: true, missingTools: true } turns them on, for a server whose instances list and serve their own tools, the same to every request'
      return line
    }
    const on = [extras.sessions && SESSION_ID, extras.intent && INTENT, extras.missingTools && state.options.inputsMode !== 'off' && MISSING_TOOLS].filter((name): name is string => typeof name === 'string')
    line.extras = {
      on,
      from: "the server's own tool list: every page, read before anything changes and again after it changes; a call waits at most 1 second for it",
      for: 'instances that list the same tools to every request they serve',
      ...(on.includes(MISSING_TOOLS) && state.options.inputsMode === 'asked' ? { get_more_tools: 'only where the inputs function answers true for it' } : {}),
    }
    return line
  }
  const extras = state.highExtras
  if (!anyExtra(extras)) {
    line.extras = 'off'
    return line
  }
  const registered = declared(state.high, '_registeredTools')
  const names = typeof registered === 'object' && registered !== null ? Object.keys(registered) : []
  const owners = names.map((name) => ownershipOf(state, name))
  const can = (input: string) => `${owners.filter((owner) => owner?.readable && !owner.declared.has(input)).length} of the ${names.length} tools registered so far`
  const described: Record<string, string> = blank()
  if (extras.sessions) described.session_id = can(SESSION_ID)
  if (extras.intent) described.intent = can(INTENT)
  if (extras.sessions || extras.intent) described.later = "tools registered later follow the same rules, and a tool whose listed schema can't take an input is listed as it is"
  if (extras.missingTools) {
    described.get_more_tools = names.includes(MISSING_TOOLS)
      ? 'not added: the server has its own'
      : state.options.inputsMode === 'off'
        ? "not added: its input wouldn't be recorded"
        : state.options.inputsMode === 'asked'
          ? 'added while the server has an enabled tool of its own, and the inputs function answers true for it'
          : 'added while the server has an enabled tool of its own'
  }
  if (extras.sessions) described.idLine = "added to a task's first result when its call can't be grouped by its protocol session or a conversationId"
  line.extras = described
  return line
}

/** What `identify` gave, or why it didn't. */
type Identified = { ok: true; ids: CallIds; metadata: unknown } | { ok: false; why: string }

interface Running {
  scope: CallScope
  /** When the call arrived, on the client's clock, as are all the wrapper's times (see Internal.clock). */
  started: number
  capturedAt: string
  toolName: string
  args: unknown
  /** Read once per call, from the options. */
  inputs: boolean
  results: boolean
  /** Who called and how, read when the call arrived: a reconnect meanwhile doesn't change them. */
  client: unknown
  protocolVersion: unknown
  /** The call brings what an earlier round asked for. */
  continued: boolean
  /** The session the transport established, for a client on a protocol version that has them. */
  protocolSession: string | undefined
  /** What the extras took out of the arguments, and whether the wrapper answers the call. */
  extras: Prepared
  /** How the call is grouped, decided when the tool returns. */
  session: { id: string; source: 'sent' | 'protocol' | 'issued' } | undefined
  identifyStarted: boolean
  identified: Promise<Identified> | undefined
  /**
   * identify's deadline passed: whatever it gives later is ignored. An object of its own, so what
   * waits on identify (see identified()) holds it, and not the call. `by` is that deadline, once
   * the tool has returned: an answer that settles past it is late too, though the timer that
   * marks it hasn't run yet (the event loop was blocked meanwhile).
   */
  late: { passed: boolean; by: number | undefined }
  /** Aborts `identify` at its deadline. */
  abort: AbortController | undefined
}

/** How the call ended, read when it ended. */
interface Outcome {
  status: 'completed' | 'error'
  error?: NonNullable<CallRecord['error']>
  round?: 'input_required' | 'continuation'
  task?: { id: unknown; status: unknown }
  /** The result is recorded. */
  result: boolean
}

/**
 * One `tools/call`: the tool runs unchanged, inside the call's scope, and its reply or error goes
 * back as it came. The event is built after it returns. If anything in recording fails, the call
 * goes on untouched; the app's handler is only ever called once.
 */
async function onCall(state: Instrumented, original: Handler, request: unknown, extra: unknown): Promise<unknown> {
  // A tools/call handler set after this one that calls it (middleware) runs the wrapper again,
  // inside the call: that call is the outer wrapper's, recorded once there.
  if (nested(state, extra)) return original(request, extra)
  const { clock } = state.internal
  const arrived = { at: clock.now(), on: new Date().toISOString() }
  // The connection the call came on, read before any wait: its session (see protocolSession()),
  // and if its close flush starts while the call runs, the call's event, built after it, gets a
  // flush of its own.
  const connection = declared(state.low, 'transport')
  // A low-level server waits for its snapshot first, at most a second; a high-level one doesn't
  // wait at all. Then the extras' inputs come out, in the same step as the handler is called, so
  // the snapshot they're taken by can't go stale before it runs, and the request passed on never
  // has them, whatever fails later.
  if (lowOn(state)) {
    try {
      const wait = newWait()
      const reader = readerOf(state)
      while (!fresh(state.lists) && (await waitOnce(state.lists, reader, extra, wait))) {
        // Checked again above, in the step that goes on to call the handler.
      }
    } catch {
      // Without the snapshot, the call is passed on as it came.
    }
  }
  const prepared = lowOn(state) ? prepareLow(state, request, fresh(state.lists)) : prepare(state, request)
  const passed = prepared.request
  const handler: Handler = prepared.answer ? (asked) => missingToolsReply(read(read(read(asked, 'params'), 'arguments'), 'need')) : original
  let call: Running | undefined
  let calls: AsyncLocalStorage<CallScope> | undefined
  try {
    calls = state.internal.calls()
    call = start(state, prepared, extra, arrived, connection)
  } catch {
    call = undefined
  }
  if (!call || !calls) return handler(passed, extra)
  const running = call
  const highLevel = state.high !== undefined && !state.replaced
  // identify starts once the tool's own function has run its first step. On a low-level server,
  // or one whose dispatcher was replaced, the handler is the tool.
  running.scope.onToolEntry = () => startIdentify(state, running, extra)

  // The app's handler is called exactly once: in the call's scope, or, if entering it fails, as it is.
  // While it runs, a wrapper entered inside it for the same request passes it through (see nested()).
  dispatching.set(running.scope, { state, extra })
  let pending: Promise<unknown>
  let invoked = false
  try {
    pending = Promise.resolve(
      calls.run(running.scope, () => {
        invoked = true
        return handler(passed, extra)
      }),
    )
  } catch (error) {
    pending = invoked ? Promise.reject(error) : Promise.resolve().then(() => handler(passed, extra))
  }
  if (!highLevel) startIdentify(state, running, extra)

  let result: unknown
  let thrown: { error: unknown } | undefined
  try {
    result = await pending
  } catch (error) {
    thrown = { error }
  }
  dispatching.delete(running.scope)
  const finished = clock.now()

  let reply = result
  let building = false
  try {
    running.scope.returned = true
    // The SDK answered without running the tool: identify starts now.
    startIdentify(state, running, extra)
    const outcome = outcomeOf(state, running, result, thrown, extra)
    reply = group(running, outcome, result)
    const { waitUntil, flushBeforeReply } = state.options
    // flush() inside the tool promises its event before the reply, whatever the options.
    const replyWaits = running.scope.flushRequested || (!waitUntil && flushBeforeReply)
    const replyBy = finished + state.internal.flushTimeoutMs
    // With waitUntil, the sending is handed to it whether or not the reply waits too: what the
    // reply doesn't wait for goes on after it, up to WAIT_UNTIL_MS.
    const sendBy = replyWaits && !waitUntil ? replyBy : finished + WAIT_UNTIL_MS
    // Building the event (waiting for identify) is what flush() waits for; sending, the rest. The
    // build lets go of the call's data once it is done (see letGo()).
    building = true
    // identify waits as long as the reply does, its second at most. A flushTimeoutMs of 0 waits for
    // nothing: the reply keeps its turn, and identify its second, the event going once it answers.
    const identifyBy = replyWaits && state.internal.flushTimeoutMs > 0 ? replyBy : finished + WAIT_UNTIL_MS
    const built = build(state, running, outcome, result, finished, identifyBy)
    state.internal.track(running.scope.callId, built.then(() => {}))
    // The connection's close flush has started (its client shut it while the tool ran): this event
    // wasn't waiting to be built then, so it gets one too.
    if (typeof connection === 'object' && connection !== null && closing.has(connection)) flushQueued(state)
    const sent = replyWaits || waitUntil ? sending(state.internal, built, running.scope.turns, sendBy, waitUntil) : undefined
    if (sent && replyWaits) await within(clock, sent, replyBy)
  } catch {
    // Dropped before it was built: nothing else lets go of the call's data.
    if (!building) letGo(running.scope)
    try {
      state.internal.report({ code: 'internal_error', message: 'Recording a tool call failed unexpectedly; its event was dropped.' })
      // Nothing was handed over: what is, settles once that report has reached onError.
      const { waitUntil } = state.options
      if (waitUntil) handOver(waitUntil, state.internal.delivered())
    } catch {
      // Nowhere to report it.
    }
  }
  if (thrown) throw thrown.error
  return reply
}

/** Calls whose handler is running, by their scope: the server's wrapper, and the request's context. */
const dispatching = new WeakMap<CallScope, { state: Instrumented; extra: unknown }>()

/**
 * This server's wrapper is already handling this request, further out: the current call scope is
 * its, while its handler runs, and the context is the one it passed on, or one for the same
 * request ID (middleware may pass a copy). Another request, such as one a tool makes through a
 * client, comes with a context and an ID of its own.
 */
function nested(state: Instrumented, extra: unknown): boolean {
  try {
    const scope = state.internal.calls().getStore()
    const outer = scope === undefined ? undefined : dispatching.get(scope)
    if (outer?.state !== state) return false
    if (outer.extra === extra) return true
    const id = requestId(extra)
    return id !== undefined && id === requestId(outer.extra)
  } catch {
    return false
  }
}

/** The JSON-RPC ID of the request a handler's context is for: SDK 1.x's `requestId`, SDK 2.x's `mcpReq.id`. */
function requestId(extra: unknown): unknown {
  const id = declared(extra, 'requestId') ?? declared(declared(extra, 'mcpReq'), 'id')
  return typeof id === 'string' || typeof id === 'number' ? id : undefined
}

function start(state: Instrumented, prepared: Prepared, extra: unknown, arrived: { at: number; on: string }, connection: unknown): Running {
  const request = prepared.request
  const params = (request as { params?: Record<string, unknown> } | null)?.params
  const toolName = typeof params?.name === 'string' ? params.name : ''
  const scope: CallScope = {
    callId: randomUUID(),
    returned: false,
    agent: undefined,
    flushRequested: false,
    turns: new Set(),
    invoked: false,
    thrown: undefined,
    toolResult: undefined,
    onToolEntry: undefined,
    released: false,
  }
  if (state.high && !state.replaced) tap(state, toolName)
  const version = protocolVersion(state, request, extra)
  return {
    scope,
    started: arrived.at,
    capturedAt: arrived.on,
    toolName,
    // What the client sent, never a polluted Object.prototype's (see strip()).
    args: own(params, 'arguments'),
    inputs: prepared.inputs ?? state.options.inputs(toolName),
    results: state.options.results(toolName),
    client: clientInfo(state.low, request, extra),
    protocolVersion: version,
    continued: continued(request, extra),
    protocolSession: prepared.grouping ? protocolSession(state, version, extra, connection) : undefined,
    extras: prepared,
    session: undefined,
    identifyStarted: false,
    identified: undefined,
    late: { passed: false, by: undefined },
    abort: undefined,
  }
}

/** Waits for `promise` until `deadline` (on `clock`), whatever happens inside it. */
async function within(clock: Internal['clock'], promise: Promise<unknown>, deadline: number): Promise<void> {
  let timer: ReturnType<typeof setTimeout> | undefined
  try {
    await Promise.race([promise, new Promise<void>((resolve) => (timer = setTimeout(resolve, Math.max(0, deadline - clock.now()))))])
  } catch {
    // Never rejects.
  } finally {
    clearTimeout(timer)
  }
}

/**
 * The call's sending: its event once built, and the turns captured inside it before the tool
 * returned (see CallScope.turns), until each is sent or dropped, or until `sendBy`. Handed to
 * `waitUntil` first, whatever the reply then does, where it settles once what was reported about
 * the call (its event refused or failed, identify failing) has reached onError too: the host may
 * end the request then. The reply doesn't wait for that. A host may hold what it was handed for as
 * long as the ingestion takes, so this holds only the client, the build, the turns and the
 * deadline: nothing of the call itself (see letGo()). Never rejects.
 */
function sending(internal: Internal, built: Promise<string | undefined>, turns: Set<object>, sendBy: number, waitUntil: Settings['waitUntil']): Promise<void> {
  const { clock } = internal
  const sent = within(clock, built.then((callId) => internal.settled(callId, Math.max(0, sendBy - clock.now()), turns)), sendBy)
  if (waitUntil) handOver(waitUntil, sent.then(() => internal.delivered()))
  return sent
}

function handOver(waitUntil: (promise: Promise<unknown>) => void, promise: Promise<void>): void {
  try {
    silence(waitUntil(promise))
  } catch {
    // The host refused it: sending carries on in the background.
  }
}

/**
 * On a high-level server, wraps the tool's own function, in place: the SDK turns what it throws
 * into an isError result, and validates its output after it returns, so this is where the wrapper
 * sees the tool itself. 1.x dispatches `handler` (1.24+), `callback`, or a task handler's
 * `createTask`; 2.x, `executor`, around its `handler`. Each is wrapped once. When the tool
 * returns, the call's capture() boundary passes, whatever the SDK still does with the result.
 * What it answers is adopted as that SDK adopts it (see adopted()): 1.x awaits Promise.resolve()
 * of it, and 2.x's executor, an async function, returns it to its own resolve function.
 */
function tap(state: Instrumented, toolName: string): void {
  try {
    // Only a tool the server registered, by its definition's own fields (or a class's): SDK 1.x's
    // has no `executor`, which a polluted Object.prototype mustn't supply (see declared()).
    const tool = own(declared(state.high, '_registeredTools'), toolName) as Record<string, unknown> | undefined
    if (!tool || typeof tool !== 'object') return
    const executor = declared(tool, 'executor')
    if (typeof executor === 'function') {
      // 2.x: executor is an async wrapper made at registration around the handler, so it settles a
      // microtask after the tool returns. The handler is tapped instead, and called the way 2.x's
      // executor calls it: unbound, with the arguments when the tool had an input schema then.
      if (marked(executor)) return
      const handler = declared(tool, 'handler')
      if (typeof handler !== 'function') return wrapIn(state, tool, 'executor', executor, true)
      const wrapped = tapped(state, handler as (...args: unknown[]) => unknown, true)
      const withArgs = Boolean(declared(tool, 'inputSchema'))
      const replacement = async (args: unknown, ctx: unknown) => (withArgs ? wrapped(args, ctx) : wrapped(ctx))
      Object.defineProperty(replacement, TAPPED, blank({ value: true }))
      tool.executor = replacement
      return
    }
    for (const key of ['handler', 'callback']) {
      const fn = declared(tool, key)
      if (typeof fn === 'function') return wrapIn(state, tool, key, fn, false)
      const createTask = key === 'handler' && typeof fn === 'object' && fn !== null ? declared(fn, 'createTask') : undefined
      if (typeof createTask === 'function') return wrapIn(state, fn as Record<string, unknown>, 'createTask', createTask, false)
    }
  } catch {
    // Untapped: a throw then reads as the tool's own error.
  }
}

/** Puts `fn`, the function the definition's `key` held when tap() read it, in its place, watched (see tapped()); never read again. */
function wrapIn(state: Instrumented, holder: Record<string, unknown>, key: string, fn: unknown, resolving: boolean): void {
  if (marked(fn)) return
  holder[key] = tapped(state, fn as (...args: unknown[]) => unknown, resolving)
}

/** The wrapper's own mark on a function it put in place, read and silenced (a Proxy's answer may be a promise). */
function marked(fn: unknown): boolean {
  return read(fn, TAPPED) === true
}

/**
 * The tool's own function, watched: when it is entered, what it returns or throws, and when. Each
 * run of it has its own capture() boundary: SDK 2.x's legacy shim runs it once per round, all in
 * one call, so a capture() in a later round is still inside its run, and one left over from an
 * earlier round isn't. A tool's function run from inside another's run (the app calling another
 * registered tool's handler from a tool) isn't the call's tool: it runs as it is, inside that run,
 * and records nothing on the call. Only a context inside a run, or a continuation made there, holds
 * the run's scope, so that holds once the run has returned too: a function that a continuation the
 * tool left running calls while the call goes on (as the SDK checks the result, say) isn't the
 * call's tool either. What it answers is adopted as the SDK calling it adopts it: as `await`
 * does, or, `resolving` (SDK 2.x's executor), as a resolve function does (see adopted()).
 */
function tapped(state: Instrumented, fn: (...args: unknown[]) => unknown, resolving: boolean): (...args: unknown[]) => unknown {
  const calls = state.internal.calls()
  const wrapped = function (this: unknown, ...params: unknown[]) {
    let scope: CallScope | undefined
    let run: CallScope | undefined
    try {
      scope = calls.getStore()
      if (scope && runs.has(scope)) scope = undefined
      if (scope) {
        scope.invoked = true
        run = invocation(scope)
      }
    } catch {
      scope = undefined
    }
    const entered = () => {
      try {
        scope?.onToolEntry?.()
      } catch {
        // identify then waits for the call's end.
      }
    }
    // A run that ends once the call has let go (from work the tool left running) keeps nothing in
    // its scope, which a turn captured inside the call may hold until it is sent.
    const done = (value: unknown) => {
      if (scope && !scope.released) scope.toolResult = { value }
      if (run) run.returned = true
      return value
    }
    const failed = (error: unknown): never => {
      if (scope && !scope.released) scope.thrown = { error }
      if (run) run.returned = true
      throw error
    }
    // The function is called exactly once: in its run's scope, or, if entering it fails, in the call's.
    let called = false
    const call = () => {
      called = true
      return fn.apply(this, params)
    }
    let out: unknown
    try {
      try {
        out = run ? calls.run(run, call) : call()
      } catch (error) {
        if (called) throw error
        out = call()
      }
    } catch (error) {
      entered()
      return failed(error)
    }
    entered()
    // Adopted in the run's scope, as the SDK adopts the answer where it called the tool: the
    // `then` that runs, in a job, is the tool's own code, inside this run's capture() boundary as
    // the function was. Exactly once too: adopted() itself never throws, so only entering the
    // scope can, and then it is adopted in the call's.
    let adopting: Promise<unknown> | undefined
    let tried = false
    const adopt = () => {
      tried = true
      return adopted(out, resolving)
    }
    try {
      adopting = run ? calls.run(run, adopt) : adopt()
    } catch {
      if (!tried) adopting = adopt()
    }
    return adopting ? adopting.then(done, failed) : done(out)
  }
  Object.defineProperty(wrapped, TAPPED, blank({ value: true }))
  return wrapped
}

/**
 * The scope one run of the tool's own function sees: the call's, read and written through, except
 * its boundary, which closes when this run returns or the whole call ends.
 */
function invocation(call: CallScope): CallScope {
  let ended = false
  const run: CallScope = {
    get callId() {
      return call.callId
    },
    get returned() {
      return ended || call.returned
    },
    set returned(value: boolean) {
      if (value) ended = true
    },
    get agent() {
      return call.agent
    },
    set agent(value) {
      call.agent = value
    },
    get flushRequested() {
      return call.flushRequested
    },
    set flushRequested(value: boolean) {
      call.flushRequested = value
    },
    get turns() {
      return call.turns
    },
    get invoked() {
      return call.invoked
    },
    set invoked(value: boolean) {
      call.invoked = value
    },
    get thrown() {
      return call.thrown
    },
    set thrown(value) {
      call.thrown = value
    },
    get toolResult() {
      return call.toolResult
    },
    set toolResult(value) {
      call.toolResult = value
    },
    get onToolEntry() {
      return call.onToolEntry
    },
    set onToolEntry(value) {
      call.onToolEntry = value
    },
    get released() {
      return call.released
    },
    set released(value: boolean) {
      call.released = value
    },
  }
  runs.add(run)
  return run
}

/** The scopes `invocation()` made: each a run of a tool's own function. */
const runs = new WeakSet<CallScope>()

/**
 * Starts `identify`, once, within the client's hook limit: past it, the hook isn't called. It runs
 * as a callback, outside the call: a capture() from it is never the call's agent part. Its signal
 * fires at its deadline; one that ignores it keeps its slot until it settles. The ID fields it
 * returns become the last ones known as soon as it succeeds. Never rejects.
 */
function startIdentify(state: Instrumented, call: Running, extra: unknown): void {
  try {
    begin(state, call, extra)
  } catch {
    call.identified = Promise.resolve({ ok: false, why: 'could not be started' })
  }
}

function begin(state: Instrumented, call: Running, extra: unknown): void {
  if (call.identifyStarted) return
  call.identifyStarted = true
  const hook = state.options.identify
  if (!hook) return
  const { internal } = state
  // After shutdown() it isn't called: the call's event is dropped, which the client reports once,
  // as shutdown, never as a hook limit it didn't reach (parity-25).
  if (internal.closed?.() === true) return
  const slots = internal.identifying
  if (slots.active >= internal.maxConcurrentHooks) {
    call.identified = Promise.resolve({ ok: false, why: 'skipped at the hook limit' })
    return
  }
  const abort = new AbortController()
  call.abort = abort
  slots.active++
  let running: Promise<unknown>
  try {
    // Adopted as `await` adopts it (see follow()): an own `then` of a native promise it answers
    // with is never run by the SDK, at once, as the methods called on it below would run it.
    // Adopted inside the callback: the `then` that runs, in a job, is identify's own code, run
    // as identify is, one level deeper and outside the call.
    running = follow(internal.callback(() => Promise.resolve(hook({ toolName: call.toolName, args: call.args, context: extra, signal: abort.signal }))))
  } catch (error) {
    running = Promise.reject(error)
  }
  call.identified = identified({ identifying: slots, identified: internal.identified, validId: internal.validId, clock: internal.clock }, running, call.late)
}

/** What of the client waits on a running `identify`: the hook limit's count, the ID fields last learned, the ID check and the clock. */
type IdentifyBooks = Pick<Internal, 'identifying' | 'identified' | 'validId' | 'clock'>

/**
 * What a running `identify` gives, checked as soon as it settles. One that ignores its signal may
 * run on long after its call is gone: what waits on it holds `books` and the deadline's flag,
 * never the call (its arguments), its request, the server or the client.
 */
function identified(books: IdentifyBooks, running: Promise<unknown>, late: Running['late']): Promise<Identified> {
  const slots = books.identifying
  return running
    .finally(() => {
      slots.active--
      // Checked as it settles, as by the timer (0.1's rule for hooks): past its deadline, it is
      // late, even where the timer hasn't run yet because the event loop was blocked meanwhile.
      if (late.by !== undefined && books.clock.now() >= late.by) late.passed = true
    })
    .then(
      (value) => {
        const checked = checkIdentity(books.validId, value)
        // Past its deadline the call has already gone without it: its IDs teach nothing.
        if (checked.ok && !late.passed) books.identified.keys = Object.keys(checked.ids)
        return checked
      },
      (): Identified => ({ ok: false, why: 'threw' }),
    )
    // Checking the result failing is a failure too, handled at once, not when the call ends.
    .catch((): Identified => ({ ok: false, why: 'could not be checked' }))
}

function checkIdentity(validId: Internal['validId'], value: unknown): Identified {
  if (value == null) return { ok: true, ids: blank(), metadata: undefined }
  if (typeof value !== 'object') return { ok: false, why: 'returned something other than an object' }
  // Its own fields, or its class's getters, never Object.prototype's (see declared()). The IDs go
  // into an object without a prototype too, so one it lacks is never a polluted prototype's.
  const read = (key: string): { value: unknown } | undefined => {
    try {
      if (!definedBelowObjectPrototype(value, key)) return { value: undefined }
      const item = (value as Record<string, unknown>)[key]
      silence(item)
      return { value: item }
    } catch {
      return undefined
    }
  }
  // Every field read, and its promises silenced, before any is checked.
  const keys = ['orgId', 'userId', 'conversationId'] as const
  const items = keys.map((key) => read(key))
  const metadata = read('metadata')
  const ids: CallIds = blank()
  for (const [index, key] of keys.entries()) {
    const item = items[index]
    if (!item) return { ok: false, why: 'threw reading its result' }
    if (item.value == null || item.value === '') continue
    if (!validId(item.value)) return { ok: false, why: `returned an invalid ${key}` }
    ids[key] = item.value as string
  }
  return { ok: true, ids, metadata: metadata?.value }
}

/**
 * Waits for `identify`, at most until `deadline` (on `clock`). One still running then is aborted;
 * one that answered in time never sees its signal fire. One that answered past it is late, though
 * the deadline's timer hadn't run when its answer was seen.
 */
async function identityBy(clock: Internal['clock'], call: Running, deadline: number): Promise<Identified | undefined> {
  if (!call.identified) return undefined
  // For identified(), which checks an answer against it as it settles.
  call.late.by = deadline
  let timer: ReturnType<typeof setTimeout> | undefined
  const late = new Promise<Identified>((resolve) => {
    timer = setTimeout(() => {
      call.late.passed = true
      resolve({ ok: false, why: 'took longer than its deadline' })
      // Only from the timer: an identify that has settled has already won the race, since what
      // follows its promise runs in microtasks, before any timer, and cleared this one.
      try {
        call.abort?.abort()
      } catch {
        // Only where a runtime lets a listener's error out of abort(). Node and browsers report it
        // themselves instead (Node as an uncaught exception), as for any AbortSignal.
      }
    }, Math.max(0, deadline - clock.now()))
  })
  try {
    const answer = await Promise.race([call.identified, late])
    // It settled past its deadline, before the timer ran (see identified()).
    return call.late.passed ? { ok: false, why: 'took longer than its deadline' } : answer
  } finally {
    clearTimeout(timer)
  }
}

/**
 * Waits for `identify` (at most a second after the tool returned, and never past `deadline`),
 * builds the call's event and hands it to the client. Resolves to the event's ID once it is
 * queued, or undefined if it couldn't be built, which is reported. Either way, it then lets go of
 * the call's data (see letGo()). Never rejects.
 */
async function build(state: Instrumented, call: Running, outcome: Outcome, result: unknown, finished: number, deadline: number): Promise<string | undefined> {
  const { internal } = state
  try {
    const identified = await identityBy(internal.clock, call, Math.min(finished + IDENTIFY_WAIT_MS, deadline))
    const { record, diagnostic } = buildRecord(state, call, outcome, result, finished, identified)
    if (diagnostic === undefined) internal.record(record)
    else if (internal.closed) {
      // Reported as the client admits the event, never for one it refuses for shutdown(): a call
      // in flight then, its event built after it, isn't claimed as sent (parity-25).
      internal.record(record, diagnostic)
    } else {
      // A client from another copy of lsquare, whose record() takes no diagnostic: as before.
      internal.report(diagnostic)
      internal.record(record)
    }
    return record.callId
  } catch {
    try {
      internal.report({ code: 'internal_error', message: 'Recording a tool call failed unexpectedly; its event was dropped.' })
    } catch {
      // Nowhere to report it.
    }
    return undefined
  } finally {
    letGo(call.scope)
  }
}

/**
 * Once the call's event is built, or dropped for good; never at the reply, while a build still to
 * come needs them. A turn captured inside the call keeps its scope (in its capture's context)
 * until that turn is sent: from here, the scope keeps the call's ID, its turns and its boundary,
 * not what the tool returned or threw, the agent part, nor what starts identify (which holds the
 * call, its arguments included). Never throws.
 */
function letGo(scope: CallScope): void {
  try {
    // Marked first: a run of the tool's own function that ends later sees it (see tapped()).
    scope.released = true
    scope.agent = undefined
    scope.thrown = undefined
    scope.toolResult = undefined
    scope.onToolEntry = undefined
  } catch {
    // Ours, and never throws; guarded all the same.
  }
}

/** The call's record, and what it reports of identify (identify_failed), for the client to report as it admits the event. */
function buildRecord(
  state: Instrumented,
  call: Running,
  outcome: Outcome,
  result: unknown,
  finished: number,
  identified: Identified | undefined,
): { record: CallRecord; diagnostic: LSquareError | undefined } {
  const { internal, low } = state
  const { scope, toolName } = call
  const agent = scope.agent
  // The record and the IDs have no prototype, since they are read here too: an ID capture() didn't
  // give, read from a polluted Object.prototype, would pass for one it did, and spare the call its
  // limited diagnostics.
  const record: CallRecord = blank<CallRecord>({
    callId: scope.callId,
    capturedAt: call.capturedAt,
    toolName,
    status: outcome.status,
    durationMs: Math.max(0, Math.round(finished - call.started)),
    ids: blank(),
    diagnostics: false,
  })

  let diagnostic: LSquareError | undefined
  // The IDs: identify's, then capture()'s inside the call, which win.
  const captured: CallIds = blank()
  for (const key of ['orgId', 'userId', 'conversationId'] as const) {
    const value = agent?.[key]
    if (value != null && value !== '') captured[key] = value as string
  }
  record.ids = blank<CallIds>({ ...(identified?.ok ? identified.ids : {}), ...captured })
  if (identified && !identified.ok) {
    // Diagnostics, unless capture() supplied every ID identify last returned (a userId before any).
    const needed = internal.identified.keys ?? ['userId']
    const supplied = needed.every((key) => internal.validId(captured[key as keyof CallIds]))
    record.diagnostics = !supplied
    diagnostic = {
      code: 'identify_failed',
      message: `identify ${identified.why}; the call was sent ${supplied ? "with capture()'s IDs" : 'as limited diagnostics'}.`,
    }
  } else if (identified?.ok && identified.metadata !== undefined) {
    record.metadata = identified.metadata
  }
  if (agent) {
    record.agent = agent
    // The turn's reference, from the turnId the event keeps: one it drops makes none.
    const turn = internal.turnId(agent.turnId)
    const conversationId = captured.conversationId
    // An orgId that isn't valid makes none either: the event is then dropped for it, as invalid_id.
    const orgId = record.ids.orgId
    if (turn !== undefined && conversationId !== undefined && internal.validId(conversationId) && (orgId === undefined || internal.validId(orgId))) {
      record.turnRef = internal.turnKey(orgId, conversationId, turn)
    }
  }

  const server = declared(low, '_serverInfo')
  if (server !== undefined) record.server = server
  if (call.client !== undefined) record.client = call.client
  if (call.protocolVersion !== undefined) record.protocolVersion = call.protocolVersion
  if (call.session) {
    record.sessionId = call.session.id
    record.sessionSource = call.session.source
  }
  // Recorded whether or not inputs are: turning the extra off is what stops it.
  if (call.extras.intent !== undefined) record.intent = call.extras.intent
  if (call.extras.answer) record.extra = 'missing_tools'
  if (call.inputs) record.input = { value: call.args }
  if (outcome.error) record.error = outcome.error
  if (outcome.round) record.round = outcome.round
  if (outcome.task) record.task = outcome.task
  if (outcome.result) record.result = { value: result }
  return { record, diagnostic }
}

/**
 * How the call ended, read as it ended: a cancellation or a reconnect later changes nothing.
 * A throw is `protocol` when it carries a JSON-RPC code, `thrown` otherwise. An isError result is
 * `thrown` when the tool's own function threw (the SDK turned it into the result), `protocol` when
 * the SDK made it (the tool never ran, or its output failed the SDK's checks), and `tool_error`
 * when the tool returned it. A request cancelled before the end is `timeout`. An isError result's
 * message is its first text block. A failed call's text, in the error and an isError result, is kept
 * only under errorText(); its category, type and code always are.
 */
function outcomeOf(state: Instrumented, call: Running, result: unknown, thrown: { error: unknown } | undefined, extra: unknown): Outcome {
  const { scope } = call
  // Without a prototype: a field it lacks (`round`, `task`, `error`) is never a polluted Object.prototype's.
  const outcome = blank<Outcome>({ status: 'completed', result: false })
  const fields = !thrown && typeof result === 'object' && result !== null ? (result as Record<string, unknown>) : undefined
  // The result's own fields, as they go to the client: never a polluted Object.prototype's.
  const field = (key: string): unknown => own(fields, key)
  if (field('resultType') === 'input_required') outcome.round = 'input_required'
  else if (call.continued) outcome.round = 'continuation'

  if (aborted(extra)) {
    outcome.status = 'error'
    outcome.error = { message: 'The request was cancelled before the tool finished.', category: 'timeout' }
    return outcome
  }
  if (thrown) {
    outcome.status = 'error'
    // Read as the SDK reads it for its JSON-RPC error reply, inherited or not (see errorOf()).
    const code = read(thrown.error, 'code')
    outcome.error = errorOf(thrown.error, Number.isSafeInteger(code) ? 'protocol' : 'thrown', true, errorText(call))
    return outcome
  }
  if (!fields) return outcome
  // The task's own fields too, as they go to the client.
  const task = field('task')
  const taskId = own(task, 'taskId')
  if (typeof taskId === 'string') outcome.task = { id: taskId, status: own(task, 'status') }
  else outcome.result = call.results

  if (field('isError') === true) {
    outcome.status = 'error'
    outcome.result &&= errorText(call)
    if (scope.thrown) {
      outcome.error = errorOf(scope.thrown.error, 'thrown', false, errorText(call))
    } else {
      const highLevel = state.high !== undefined && !state.replaced
      const returned = read(scope.toolResult?.value, 'isError') === true
      // The wrapper's own get_more_tools refuses arguments as the SDKs do, at either level.
      const category: ErrorCategory = call.extras.answer || (highLevel && (!scope.invoked || !returned)) ? 'protocol' : 'tool_error'
      const error: NonNullable<CallRecord['error']> = blank({ category })
      if (errorText(call)) {
        const text = firstText(field('content'))
        if (text !== undefined) error.message = text
      }
      outcome.error = error
    }
  }
  return outcome
}

/**
 * Whether a failed call's text is recorded. It can quote what the tool would have returned, and what
 * it was sent: a validator's message does, and it can't always be told from the tool's own.
 */
function errorText(call: Running): boolean {
  return call.results && call.inputs
}

/**
 * A thrown error's details, its text only with `text`. `escaped`: it escaped the handler, and the SDK answered with a
 * JSON-RPC error, whose code and message both SDKs read as `error.code` and `error.message`,
 * inherited ones included: they are read the same way, so the event has what the client got, a
 * polluted Object.prototype's too. Otherwise the tool's own function threw, and the SDK made an
 * isError result of it: the message is the error's own or its class's (Error.prototype's
 * included), never Object.prototype's alone (see declared()), and without one the value as a
 * string, as the SDK writes it then. The type (`name`) goes to no client: always the error's own
 * or its class's.
 */
/** An error's own `name`, as a class has one: anything else could hold what the tool was sent. */
const ERROR_NAME = /^[A-Za-z_$][\w$.]{0,127}$/

function errorOf(error: unknown, category: ErrorCategory, escaped: boolean, text: boolean): NonNullable<CallRecord['error']> {
  // No prototype, as the record it goes in: its fields are added by assignment.
  const details: NonNullable<CallRecord['error']> = blank({ category })
  if ((typeof error === 'object' || typeof error === 'function') && error !== null) {
    if (text) {
      const message = escaped ? read(error, 'message') : declared(error, 'message')
      details.message = typeof message === 'string' ? message : escaped ? 'Unknown error' : asText(error)
    }
    const name = declared(error, 'name')
    if (typeof name === 'string' && ERROR_NAME.test(name)) details.type = name
    // Only an error that escaped is `protocol`, by the code its reply carries.
    if (category === 'protocol') {
      const code = read(error, 'code')
      if (typeof code === 'number' || typeof code === 'string') details.code = code
    }
  } else if (text) {
    details.message = asText(error)
  }
  return details
}

/** A thrown value as a string, as `String()` writes it; 'Unknown error' when that throws. */
function asText(value: unknown): string {
  try {
    return String(value)
  } catch {
    return 'Unknown error'
  }
}

function firstText(content: unknown): string | undefined {
  try {
    if (!Array.isArray(content)) return undefined
    const length = Math.min(Number(read(content, 'length')) || 0, 1_000)
    for (let index = 0; index < length; index++) {
      // Entries as JSON writes them, from the list's prototype too; each block's own fields.
      const block = read(content, String(index))
      if (own(block, 'type') === 'text') {
        const text = own(block, 'text')
        return typeof text === 'string' ? text : undefined
      }
    }
  } catch {
    // A list that can't be read gives no message.
  }
  return undefined
}

function aborted(extra: unknown): boolean {
  const signal = declared(extra, 'signal') ?? declared(declared(extra, 'mcpReq'), 'signal')
  return declared(signal, 'aborted') === true
}

/**
 * The call brings what an earlier round asked for: input responses, or the state the server handed
 * out to carry on with, which SDK 2.x lifts out of the request into `requestState()`.
 */
function continued(request: unknown, extra: unknown): boolean {
  const mcpReq = declared(extra, 'mcpReq')
  const params = declared(request, 'params')
  const responses = declared(mcpReq, 'inputResponses') ?? declared(params, 'inputResponses')
  if (typeof responses === 'object' && responses !== null) return true
  if (typeof declared(params, 'requestState') === 'string') return true
  const state = declared(mcpReq, 'requestState')
  if (typeof state !== 'function') return typeof state === 'string'
  try {
    const value = (state as () => unknown).call(mcpReq)
    silence(value)
    return value !== undefined
  } catch {
    return false
  }
}

/**
 * The calling app: from SDK 2.x's envelope, which it lifts out of `_meta` before handlers run;
 * from `_meta` on SDK 1.x (protocol 2026-07-28); or what it sent in `initialize`.
 */
function clientInfo(low: LowLevel, request: unknown, extra: unknown): unknown {
  const fromEnvelope = declared(envelopeOf(extra), CLIENT_INFO_KEY)
  if (fromEnvelope !== undefined) return fromEnvelope
  const fromMeta = declared(metaOf(request), CLIENT_INFO_KEY)
  if (fromMeta !== undefined) return fromMeta
  try {
    const get = declared(low, 'getClientVersion')
    if (typeof get !== 'function') return undefined
    const info = (get as () => unknown).call(low)
    silence(info)
    return info
  } catch {
    return undefined
  }
}

/**
 * The request's own version: in its envelope or `_meta` (protocol 2026-07-28), or over HTTP in its
 * `MCP-Protocol-Version` header, which a stateless server's fresh instance has without having seen
 * `initialize`. Else the one `initialize` negotiated.
 */
function protocolVersion(state: Instrumented, request: unknown, extra: unknown): unknown {
  const fromEnvelope = declared(envelopeOf(extra), PROTOCOL_VERSION_KEY)
  if (typeof fromEnvelope === 'string') return fromEnvelope
  const fromMeta = declared(metaOf(request), PROTOCOL_VERSION_KEY)
  if (typeof fromMeta === 'string') return fromMeta
  const fromHeader = versionHeader(extra)
  if (fromHeader !== undefined) return fromHeader
  if (state.protocolVersion !== undefined) return state.protocolVersion
  const negotiated = declared(state.low, '_negotiatedProtocolVersion')
  return typeof negotiated === 'string' ? negotiated : undefined
}

/** The `MCP-Protocol-Version` header: SDK 2.x hands handlers the HTTP request, SDK 1.x its headers. */
function versionHeader(extra: unknown): string | undefined {
  for (const headers of [declared(declared(declared(extra, 'http'), 'req'), 'headers'), declared(contextField(extra, 'requestInfo'), 'headers')]) {
    const value = header(headers, 'mcp-protocol-version')
    // Older SDK 1.x Node transports give a header sent twice as a list: which one was meant can't be told.
    if (typeof value === 'string') return value
  }
  return undefined
}

/** A header from a web `Headers` (its class's `get`), or from a plain object of lowercased names, as Node gives them (its own). */
function header(headers: unknown, name: string): unknown {
  const get = declared(headers, 'get')
  if (typeof get !== 'function') return own(headers, name)
  try {
    const value = (get as (name: string) => unknown).call(headers, name)
    silence(value)
    return value
  } catch {
    return undefined
  }
}

/** The request's `_meta`, as the client sent it (see sentMeta()). */
function metaOf(request: unknown): unknown {
  return sentMeta(declared(declared(request, 'params'), '_meta'))
}

/** SDK 2.x's envelope, which it lifts out of `_meta` before handlers run, as the client sent it. */
function envelopeOf(extra: unknown): unknown {
  return sentMeta(declared(declared(extra, 'mcpReq'), 'envelope'))
}

/**
 * `meta`, a request's `_meta` or the envelope SDK 2.x lifts out of it, unless it may be a copy of
 * a polluted Object.prototype's `_meta`: both SDKs' JSON-RPC parse on stdio and HTTP, and SDK
 * 2.x's lifting in memory, read a request's `_meta` through the prototype, so a request whose
 * client sent none gets that one's fields, each the very value it holds, and nothing else. What
 * Object.prototype holds is asked of its property descriptors, along the polluted object's chain,
 * running none of its code; what it gave the SDK can be known only where each is a data property
 * of an ordinary object: a getter, or a Proxy, may have answered the SDK anew, so then `meta` is
 * skipped, unless one of its fields is shown to be what the polluted one never held (see
 * held()). Otherwise only an object can show where it came from: JSON.parse makes each afresh, so
 * a field holding the very object the polluted `_meta` holds (its client info) was copied from
 * it. A string can't: a `_meta` whose fields are all strings or numbers equal to the polluted
 * one's is read as the client's, which it may be (SDK 1.x takes one holding the protocol version
 * alone), and a polluted `_meta` of those alone is read as the client's too.
 */
function sentMeta(meta: unknown): unknown {
  try {
    if (typeof meta !== 'object' || meta === null) return meta
    const polluted = held(Object.prototype, '_meta')
    if (polluted === NOT_HELD) return meta
    if (polluted !== UNKNOWN && (typeof polluted.value !== 'object' || polluted.value === null)) return meta
    let shown = false
    let unknown = polluted === UNKNOWN
    for (const key of Object.keys(meta)) {
      const value = own(meta, key)
      const field = polluted === UNKNOWN ? UNKNOWN : held(polluted.value as object, key)
      if (field === UNKNOWN) {
        unknown = true
        continue
      }
      // What the polluted `_meta` holds for it, or doesn't, is known: a copy has the very same.
      if (value !== (field === NOT_HELD ? undefined : field.value)) return meta
      if ((typeof value === 'object' && value !== null) || typeof value === 'function') shown = true
    }
    return shown || unknown ? undefined : meta
  } catch {
    return meta
  }
}

const NOT_HELD = Symbol('not held')
const UNKNOWN = Symbol('unknown')

/**
 * What `object` holds for `key`, as reading it would find it, asked of each level's property
 * descriptor, never by reading it: the value of the data property found first along its chain,
 * NOT_HELD when none has one, and UNKNOWN where a getter (which may answer anew each time it is
 * read) or a Proxy (whose traps would answer) holds it, or the chain is too deep to walk.
 */
function held(object: object, key: string): { value: unknown } | typeof NOT_HELD | typeof UNKNOWN {
  let level: object | null = object
  for (let depth = 0; depth < 32; depth++) {
    if (level === null) return NOT_HELD
    if (types.isProxy(level)) return UNKNOWN
    const descriptor = Object.getOwnPropertyDescriptor(level, key)
    if (descriptor !== undefined) return hasOwn(descriptor, 'value') ? { value: descriptor.value } : UNKNOWN
    level = Object.getPrototypeOf(level) as object | null
  }
  return UNKNOWN
}

/**
 * An object in the request's context, unless it may be the one a polluted Object.prototype holds:
 * SDK 1.x copies the `requestInfo` the transport passes with the message into it, read through
 * the prototype, so its in-memory transport, which passes none, gives that one. A transport
 * makes its own for each request, so it is ignored where it is the very object Object.prototype
 * holds, and wherever a getter holds it there (see held()), which may have given the SDK a new
 * one: no request info, while the pollution lasts.
 */
function contextField(extra: unknown, key: string): unknown {
  const value = declared(extra, key)
  if (typeof value !== 'object' || value === null) return value
  try {
    const polluted = held(Object.prototype, key)
    if (polluted === NOT_HELD) return value
    return polluted !== UNKNOWN && value !== polluted.value ? value : undefined
  } catch {
    return undefined
  }
}

function serverName(low: LowLevel): string | undefined {
  const name = declared(declared(low, '_serverInfo'), 'name')
  return typeof name === 'string' ? name : undefined
}
