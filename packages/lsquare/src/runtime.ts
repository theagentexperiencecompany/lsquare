import { definedBelowObjectPrototype, silence } from './json.js'
import type { Logger } from './types.js'

/**
 * One field of the runtime's object `value`: its own or its class's, never Object.prototype's,
 * which the global object inherits from, so a polluted `Bun` can't name Bun as a Node process's
 * runtime. Silenced as it is read: a getter (the app's own `Bun`, say) may give a rejected
 * promise, which nothing else would handle. Throws what a getter throws: callers guard.
 */
function field(value: unknown, key: string): unknown {
  if ((typeof value !== 'object' && typeof value !== 'function') || value === null || !definedBelowObjectPrototype(value, key)) return undefined
  const item = (value as Record<string, unknown>)[key]
  silence(item)
  return item
}

/**
 * A field of the runtime's own objects, along `path` from the global object (`navigator`, `Bun`,
 * `Deno`, `process`, `fetch`, `console`), each read as field() reads one. Throws what a getter
 * throws: callers guard.
 */
export function global(...path: string[]): unknown {
  let value: unknown = globalThis
  for (const key of path) {
    value = field(value, key)
    if (value === undefined) return undefined
  }
  return value
}

/**
 * Calls `value`'s method `name` with `args`, the method read as field() reads it, and returns what
 * it returns, silenced; undefined, calling nothing, when it isn't a function. Throws what the
 * method, or reading it, throws: callers guard.
 */
function invoke(value: unknown, name: string, args: unknown[]): unknown {
  const method = field(value, name)
  if (typeof method !== 'function') return undefined
  const result: unknown = Reflect.apply(method, value, args)
  silence(result)
  return result
}

export function isWorkers(): boolean {
  try {
    return global('navigator', 'userAgent') === 'Cloudflare-Workers'
  } catch {
    return false
  }
}

/** `sdk.runtime`: the runtime and its major version (Bun's minor too, before 2.0), so bugs can be traced to one. */
export function runtimeName(): string | undefined {
  try {
    if (isWorkers()) return 'workers'
    const bun = global('Bun', 'version')
    if (typeof bun === 'string') return `bun/${bun.split('.').slice(0, 2).join('.')}`
    const deno = global('Deno', 'version', 'deno')
    if (typeof deno === 'string') return `deno/${deno.split('.')[0]}`
    const node = global('process', 'versions', 'node')
    if (typeof node === 'string') return `node/${node.split('.')[0]}`
  } catch {
    // An odd global: leave the runtime out.
  }
  return undefined
}

/**
 * The runtime's `fetch`, for a client the app gives none: read only then, as global() reads it
 * (never Object.prototype's, so a runtime without one isn't given a polluted one), guarded.
 * Called with the global object as `this`, as Workers' needs ("Illegal invocation" otherwise),
 * through Reflect.apply, which reads nothing of it, where `bind` would read its `length` and `name`.
 */
export function runtimeFetch(): typeof fetch | undefined {
  try {
    const native = global('fetch')
    if (typeof native !== 'function') return undefined
    return ((input: unknown, init: unknown) => Reflect.apply(native, globalThis, [input, init])) as typeof fetch
  } catch {
    return undefined
  }
}

/** Whether `logger` is the runtime's own `console`, read as global() reads it. Never throws. */
export function isConsole(logger: unknown): boolean {
  try {
    return logger === global('console')
  } catch {
    return false
  }
}

/**
 * Never writes to stdout, which carries a stdio server's MCP messages: standard error where the
 * runtime has one (Node, Bun), and the platform's logs through `console` elsewhere (Workers), or
 * where standard error can't be read. Like `console`, it never calls back into the SDK. Made only
 * for a client given no logger, as it first logs, and never throws: `process`, its `stderr` and
 * that one's `write` are read as global() reads them.
 */
export function defaultLogger(): Logger {
  try {
    const stderr = isWorkers() ? undefined : global('process', 'stderr')
    const write = field(stderr, 'write')
    if (typeof write === 'function') {
      const line = writer(stderr as object, write as (...args: unknown[]) => unknown)
      return {
        info: (message) => line(`${message}\n`),
        warn: (message) => line(`${message}\n`),
      }
    }
  } catch {
    // A runtime whose standard error can't be read: its console, as on Workers.
  }
  return {
    info: (message) => toConsole('info', message),
    warn: (message) => toConsole('warn', message),
  }
}

/** A line for the platform's logs: `console` and its method read as each line is written, as global() reads them. */
function toConsole(level: 'info' | 'warn', message: string): void {
  try {
    invoke(global('console'), level, [message])
  } catch {
    // Nowhere to write it.
  }
}

/**
 * Writes so that a failing stream (EPIPE once stderr's reader has gone) can't crash the app: Node
 * emits a write's error on the stream, then 'close', and an 'error' nobody listens to is thrown.
 * Like Node's console, it listens only when the app doesn't: during each write, and from a write
 * that fails until the 'close' after it (a file stream emits both only once it has closed its
 * file), with one listener however many fail (Node's console.error crashes on its second write to
 * a closed stderr). While it listens, it also hears the app's own errors on that failing stream;
 * the app's own listeners behave as before.
 *
 * `write` is the method defaultLogger() read, never read again. The stream's other methods
 * (`listenerCount`, `on`, `once`, `removeListener`) are read as each is needed, and they, what
 * they return and what `write` returns are silenced (see invoke()).
 */
function writer(stream: object, write: (...args: unknown[]) => unknown): (text: string) => void {
  const ignore = () => {}
  let listening = false
  /** A write failed since the last 'close': listen on. */
  let failed = false
  const call = (name: string, ...args: unknown[]): unknown => {
    try {
      return invoke(stream, name, args)
    } catch {
      return undefined
    }
  }
  const count = (): number | undefined => {
    const listeners = call('listenerCount', 'error')
    return typeof listeners === 'number' ? listeners : undefined
  }
  const stop = () => {
    listening = false
    call('removeListener', 'error', ignore)
    call('removeListener', 'close', closed)
  }
  // A stdio stream emits its error and 'close' in the same turn as the failure, a file stream
  // later: one more turn, in case a write that failed meanwhile has its own still to come.
  const closed = () => {
    failed = false
    unref(setTimeout(() => !failed && stop(), 0))
  }
  const written = (error?: unknown) => {
    try {
      silence(error)
      if (!error) return
      failed = true
      if (listening || count() !== 0) return
      listening = true
      call('on', 'error', ignore)
      call('on', 'close', closed)
    } catch {
      // Ignore.
    }
  }
  return (text) => {
    // For an error emitted while writing, taken off once the write returns.
    const guard = !listening && count() === 0 ? ignoreOnce(call) : undefined
    try {
      silence(Reflect.apply(write, stream, [text, written]))
    } finally {
      guard?.()
    }
  }
}

/** A one-time 'error' listener, through `call` (see writer()); returns what takes it off if it hasn't run. */
function ignoreOnce(call: (name: string, ...args: unknown[]) => unknown): () => void {
  const listener = () => {}
  call('once', 'error', listener)
  return () => {
    call('removeListener', 'error', listener)
  }
}

function unref<T>(timer: T): T {
  ;(timer as { unref?: () => void }).unref?.()
  return timer
}
