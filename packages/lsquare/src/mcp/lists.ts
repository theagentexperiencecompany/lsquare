// A low-level server's tool list as the extras read it (spec section 4, "Low-level servers"): one
// snapshot per instance, read from its own tools/list handler, every page, and kept until
// something says the list changed.
import { AsyncResource } from 'node:async_hooks'
import type { Internal } from '../client.js'
import { listedOwnership, MISSING_TOOLS, type Ownership } from './extras.js'
import { put } from '../json.js'
import { adopted, declared, own, read, silence } from './read.js'

const MAX_PAGES = 100
const MAX_TOOLS = 10_000
/** How long a call waits for the snapshot, from the start of the read it waits on. */
const WAIT_MS = 1_000
/** A read still running after this is given up. */
const GIVE_UP_MS = 10_000
/** After a read fails or is given up, none starts for this long, unless the server sends list_changed. */
const PAUSE_MS = 60_000

export interface Snapshot {
  /** Each tool's ownership of the extras' inputs, by name. */
  tools: ReadonlyMap<string, Ownership>
  /** The server has a tool of its own called get_more_tools. */
  ownMissingTools: boolean
}

interface Reading {
  /** The generation it was started at: a later one discards it. */
  generation: number
  started: number
  abort: AbortController
  done: Promise<void>
  /**
   * Gives it up, GIVE_UP_MS after it started: by its own timer or, where that timer went (on
   * Workers, with the request that started it; or as an app switched its fake timers), by the
   * first call to find it past then. Only once, while it's the read running.
   */
  giveUp: () => void
}

/** What an instance keeps of its tool list. */
export interface Lists {
  /** The client's clock (see Internal.clock): the times here are on it. */
  clock: Internal['clock']
  /** Bumped by everything that says the list changed. */
  generation: number
  installed: { generation: number; snapshot: Snapshot } | undefined
  reading: Reading | undefined
  /** No read starts before this. */
  pausedUntil: number
  /**
   * Wakes the requests waiting now at the next mark, as it discards the read they wait on: each
   * wait's own, taken out as that wait ends, so none outlives it (see waitOnce()).
   */
  waiting: Set<() => void>
}

export interface Reader {
  /** The instance's own tools/list handler, as it is now, or undefined. */
  handler(): ((request: unknown, context: unknown) => unknown) | undefined
  /** Runs the app's code as a callback, outside any call. */
  callback<T>(run: () => T): T
}

export function newLists(clock: Internal['clock']): Lists {
  return { clock, generation: 0, installed: undefined, reading: undefined, pausedUntil: 0, waiting: new Set() }
}

/**
 * Something says the list changed: the snapshot and any read running are out of date. Only the
 * server's own list_changed lifts a pause, as a failed read may have failed for what it changed.
 */
export function mark(lists: Lists, listChanged: boolean): void {
  lists.generation++
  if (listChanged) lists.pausedUntil = 0
  const waiting = [...lists.waiting]
  lists.waiting.clear()
  for (const wake of waiting) wake()
  try {
    lists.reading?.abort.abort()
  } catch {
    // Only the wrapper's own listeners are on a read's signal (see onePage): guarded all the same.
  }
}

/** The snapshot, if it's of the current generation. */
export function fresh(lists: Lists): Snapshot | undefined {
  return lists.installed?.generation === lists.generation ? lists.installed.snapshot : undefined
}

/** How long a request has waited for the snapshot, and how many reads. */
export interface Wait {
  /** 1 second after the start of the first read it waited on. */
  deadline: number | undefined
  rounds: number
}

export function newWait(): Wait {
  return { deadline: undefined, rounds: 0 }
}

/**
 * Waits once for a snapshot of the current generation: joins its read, or starts one with the
 * context of the request that needs it, until the read ends, the list changes or the request's
 * deadline, 1 second after the start of the first read it waited on, passes. True when it's worth
 * checking fresh() again; false when there's nothing more to wait for. The caller checks fresh()
 * itself, in the step that uses it, so nothing can make it stale in between.
 */
export async function waitOnce(lists: Lists, reader: Reader, context: unknown, wait: Wait): Promise<boolean> {
  const { clock } = lists
  // Ten reads overtaken within a second: the list keeps changing.
  if (fresh(lists) || wait.rounds >= 10) return false
  if (wait.deadline !== undefined && clock.now() >= wait.deadline) return false
  // Its own, in place before a read starts: its handler may say the list changed the moment it's
  // called. Taken out as the wait ends: a promise kept by the instance for every wait would keep
  // each wait's reaction to it until the list next changes, which a list that fails, or keeps
  // timing out, may never do.
  let wake!: () => void
  const changed = new Promise<void>((resolve) => (wake = resolve))
  lists.waiting.add(wake)
  try {
    // Still running past its time: its timer went (see Reading.giveUp).
    if (lists.reading && clock.now() >= lists.reading.started + GIVE_UP_MS) lists.reading.giveUp()
    let reading = lists.reading
    if (!reading || reading.generation !== lists.generation) {
      if (clock.now() < lists.pausedUntil) return false
      reading = startReading(lists, reader, context)
    }
    wait.rounds++
    wait.deadline ??= reading.started + WAIT_MS
    // Past the first second of a slow read, a request doesn't wait at all.
    if (clock.now() >= wait.deadline) return false
    return await settles(clock, Promise.race([reading.done, changed]), wait.deadline)
  } finally {
    lists.waiting.delete(wake)
  }
}

/** Waits for the snapshot as waitOnce() does, then hands it, or undefined, to `use`, right after the last check. */
export async function withSnapshot<T>(lists: Lists, reader: Reader, context: unknown, use: (snapshot: Snapshot | undefined) => T): Promise<T> {
  const wait = newWait()
  while (!fresh(lists) && (await waitOnce(lists, reader, context, wait))) {
    // Checked again above.
  }
  return use(fresh(lists))
}

/** Whether `promise` settles before `deadline` (on `clock`). Never rejects. */
async function settles(clock: Internal['clock'], promise: Promise<void>, deadline: number): Promise<boolean> {
  let timer: ReturnType<typeof setTimeout> | undefined
  try {
    return await Promise.race([
      promise.then(
        () => true,
        () => true,
      ),
      new Promise<boolean>((resolve) => {
        timer = setTimeout(() => resolve(false), Math.max(0, deadline - clock.now()))
        unref(timer)
      }),
    ])
  } finally {
    clearTimeout(timer)
  }
}

function startReading(lists: Lists, reader: Reader, context: unknown): Reading {
  const reading: Reading = { generation: lists.generation, started: lists.clock.now(), abort: new AbortController(), done: Promise.resolve(), giveUp: () => {} }
  lists.reading = reading
  reading.done = run(lists, reading, reader, context)
  return reading
}

/** Reads the snapshot and installs it if nothing changed meanwhile and it wasn't given up. Never rejects. */
async function run(lists: Lists, reading: Reading, reader: Reader, context: unknown): Promise<void> {
  let timer: ReturnType<typeof setTimeout> | undefined
  try {
    const collecting = collect(reader, context, reading.abort.signal)
    collecting.catch(() => {})
    const givenUp = new Promise<undefined>((resolve) => {
      // Done here, not after the race: from another request (Workers), what follows `resolve` may
      // never run.
      reading.giveUp = () => {
        resolve(undefined)
        if (lists.reading !== reading) return
        lists.reading = undefined
        try {
          reading.abort.abort()
        } catch {
          // Only the wrapper's own listeners are on a read's signal (see onePage): guarded all the same.
        }
        failed(lists, reading, context)
      }
      timer = setTimeout(reading.giveUp, GIVE_UP_MS)
      unref(timer)
    })
    const snapshot = await Promise.race([collecting, givenUp])
    if (snapshot !== undefined && lists.reading === reading && lists.generation === reading.generation) {
      lists.installed = { generation: reading.generation, snapshot }
    }
  } catch {
    if (lists.reading === reading) failed(lists, reading, context)
  } finally {
    clearTimeout(timer)
    if (lists.reading === reading) lists.reading = undefined
  }
}

/**
 * A failed read pauses new ones for a minute. One overtaken meanwhile, or whose starting request
 * was cancelled (sending through that request's functions can fail then), is only discarded.
 */
function failed(lists: Lists, reading: Reading, context: unknown): void {
  if (lists.generation !== reading.generation || cancelled(context)) return
  lists.pausedUntil = lists.clock.now() + PAUSE_MS
}

/** Every page of the instance's own list, read with a copy of the starting request's context. */
async function collect(reader: Reader, context: unknown, signal: AbortSignal): Promise<Snapshot> {
  const handler = reader.handler()
  if (typeof handler !== 'function') throw new Error('The server has no tools/list handler.')
  const tools = new Map<string, Ownership>()
  const cursors = new Set<string>()
  let ownMissingTools = false
  let cursor: string | undefined
  for (let page = 0; page < MAX_PAGES; page++) {
    if (signal.aborted) throw new Error('The read was given up.')
    const request = { method: 'tools/list', params: cursor === undefined ? {} : { cursor } }
    const listed = await onePage(reader, handler, request, context, signal)
    // Read so that a throw fails the read: a cursor that can't be read is no last page.
    const entries = field(listed, 'tools')
    if (!Array.isArray(entries)) throw new Error('A page has no list of tools.')
    // Read once, and silenced, through get, as JSON reads a list's length: a Proxy's answer is the
    // app's code, and this read the wrapper's alone.
    const length = read(entries, 'length')
    if (typeof length !== 'number' || !(tools.size + length <= MAX_TOOLS)) throw new Error('The list has too many tools.')
    for (let index = 0; index < length; index++) {
      // Entries as JSON writes them, from the list's prototype too; each entry's own fields.
      const tool = read(entries, String(index))
      const name = own(tool, 'name')
      if (typeof name !== 'string' || tools.has(name)) throw new Error('A tool has no name, or the name of another.')
      tools.set(name, listedOwnership(own(tool, 'inputSchema')))
      if (name === MISSING_TOOLS) ownMissingTools = true
    }
    const next = field(listed, 'nextCursor')
    if (next === undefined || next === null) return { tools, ownMissingTools }
    if (typeof next !== 'string' || cursors.has(next)) throw new Error('A cursor came back twice.')
    cursors.add(next)
    cursor = next
  }
  throw new Error('The list has too many pages.')
}

/**
 * One page, with a cancellation signal of its own, as a request's own would be: the read's abort
 * reaches it only while its handler runs. It is delivered from a timer that does nothing once the
 * page has returned, since the app's code chained on the handler's own promise runs before this
 * `finally`: a list change made there must not abort a page that has returned.
 *
 * The timer is set in the async context the handler was called in, whichever request changed the
 * list, so the app's listeners see the app's own AsyncLocalStorage (request, tenant, trace) as the
 * handler did. Where that context can't be entered (Workers: another request's), the page isn't
 * aborted at all: its listeners would run in the other request's context, and could act on its
 * tenant or its I/O.
 */
async function onePage(reader: Reader, handler: (request: unknown, context: unknown) => unknown, request: unknown, context: unknown, signal: AbortSignal): Promise<unknown> {
  const own = new AbortController()
  let returned = false
  let page: AsyncResource | undefined
  const deliver = () => {
    if (returned) return
    try {
      own.abort()
    } catch {
      // Only where a runtime lets a listener's error out of abort(). Node and browsers report it
      // themselves instead (Node as an uncaught exception), as for any AbortSignal.
    }
  }
  const stop = () => {
    enter(page, () => unref(setTimeout(deliver, 0)))
  }
  signal.addEventListener('abort', stop)
  try {
    let answer: unknown
    const adopting = reader.callback(() => {
      page = new AsyncResource('LSquareListPage')
      answer = handler(request, listingContext(context, own.signal))
      // Adopted as the SDK's dispatch adopts the handler's answer, by a resolve function (see
      // adopted()), so the snapshot is what the client is listed: an own `then` of a native
      // promise runs here too, in a job. Inside the callback: that `then` is the handler's own
      // code, run as the handler is, one level deeper and outside any call.
      return adopted(answer, true)
    })
    return adopting ? await adopting : answer
  } finally {
    returned = true
    signal.removeEventListener('abort', stop)
  }
}

/**
 * A copy of the starting request's context for reading the list: its authorization, transport and
 * send functions, marked as a tools/list request, without the call's own _meta and multi-round
 * details, and with the page's own cancellation signal. SDK 1.x's context is flat; 2.x's keeps the
 * request's own fields in mcpReq, whose requestState() is a method its handlers may call.
 */
export function listingContext(context: unknown, signal: AbortSignal): unknown {
  if (typeof context !== 'object' || context === null) return { signal }
  const copy: Record<string, unknown> = { ...(context as Record<string, unknown>) }
  delete copy._meta
  // Its own, as the spread copied it: a polluted Object.prototype's isn't the context's.
  if (Object.prototype.hasOwnProperty.call(copy, 'signal')) copy.signal = signal
  const mcpReq = declared(context, 'mcpReq')
  if (typeof mcpReq === 'object' && mcpReq !== null) {
    const own: Record<string, unknown> = { ...(mcpReq as Record<string, unknown>), method: 'tools/list', signal, requestState: () => undefined }
    delete own._meta
    delete own.inputResponses
    delete own.droppedInputResponseKeys
    // Added as a literal adds it (see put()): it may be the context's class's, not in the copy.
    put(copy, 'mcpReq', own)
  }
  return copy
}

/**
 * One of a page's own properties, as it goes to the client, never one from a polluted
 * Object.prototype, letting a throw through. A promise there fails the read, and is silenced as
 * read() silences one: the read is the wrapper's, which no client asked for.
 */
function field(value: unknown, key: string): unknown {
  if ((typeof value !== 'object' && typeof value !== 'function') || value === null) return undefined
  if (!Object.prototype.hasOwnProperty.call(value, key)) return undefined
  const item = (value as Record<string, unknown>)[key]
  silence(item)
  return item
}

function cancelled(context: unknown): boolean {
  const signal = declared(context, 'signal') ?? declared(declared(context, 'mcpReq'), 'signal')
  return declared(signal, 'aborted') === true
}

/**
 * Runs `run` in `context` and returns true; or returns false, having run nothing, when the context
 * can't be entered from here: on Cloudflare Workers, one made in another request (runInAsyncScope
 * throws before calling `run`). An error thrown by `run` itself is thrown on. A copy of the client's:
 * lsquare/mcp shares no code with it (see tsup.config.ts).
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

/** A timer of the wrapper's never keeps the process alive. */
function unref(timer: unknown): void {
  try {
    ;(timer as { unref?: () => void }).unref?.()
  } catch {
    // Not Node: nothing to do.
  }
}
