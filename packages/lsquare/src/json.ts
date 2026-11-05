import { types } from 'node:util'
import type { Json } from './types.js'

const { isPromise } = types
const MAX_DEPTH = 64
export const hasOwn = (object: object, key: string) => Object.prototype.hasOwnProperty.call(object, key)

/**
 * Work allowance shared by everything one event converts. Every array slot, key and node is
 * charged before it is visited, so no input shape escapes it. For ordinary data the charge is a
 * lower bound on the JSON size, so running out means the event can't fit.
 */
export interface Budget {
  remaining: number
}

export type JsonResult = { ok: true; value: Json; lossy: boolean } | { ok: false; tooLarge: boolean }

/**
 * Gives a promise the SDK got from the app's code (a getter, a `toJSON`) a rejection handler. If it
 * rejected with nobody listening, Node would crash the app's process. The SDK never uses it: a
 * promise isn't JSON. Any native promise, whatever its prototype and from this realm or another (a
 * vm context's, or Node's own seen from a test runner's): `isPromise` reads its internal slot, so it
 * runs none of the app's code (no Proxy trap) and costs nothing for data. No other thenable's `then`
 * is ever called: it may do work (a query builder runs its query).
 */
export function silence(value: unknown): void {
  try {
    if (isPromise(value)) Promise.prototype.then.call(value, undefined, () => {})
  } catch {
    // `then` reads the promise's `constructor`, which a getter can make throw.
  }
}

/**
 * A new object without a prototype, holding `fields`. What the SDK reads from the app's objects,
 * and the records it builds an event from, are made with it, so a field one lacks reads as
 * undefined, never as a polluted Object.prototype's, and a field is added by plain assignment,
 * which no inherited setter or field that isn't writable can refuse or take. Not for the event,
 * which `beforeSend` gets as ordinary objects, nor for an object of many keys: V8 keeps one
 * without a prototype in dictionary mode, which listing its entries and JSON.stringify() pay for.
 */
export function blank<T extends object = Record<string, unknown>>(fields?: T): T {
  return Object.assign(Object.create(null) as T, fields)
}

/**
 * Adds `key` to an ordinary object the SDK built (an event, or a part of one, which `beforeSend`
 * gets as ordinary objects), as an object literal adds one (CreateDataProperty). Never through a
 * field Object.prototype has of that name: a polluted one that isn't writable
 * (Object.defineProperty's default), or a getter without a setter, would make an assignment
 * throw, and a setter would take the value instead. So where Object.prototype has one, the field
 * is defined, with a descriptor made without a prototype (a polluted `get` or `writable` can't be
 * read into it); where it has none, as for nearly every name, nothing along the object's chain
 * can take or refuse the field, and plain assignment creates it, as a literal would, at a tenth
 * of the cost. Only for an object whose prototype is Object.prototype itself (`{}`).
 */
export function put<T extends object, K extends keyof T & string>(object: T, key: K, value: T[K]): void {
  if (hasOwn(Object.prototype, key)) Object.defineProperty(object, key, { __proto__: null, value, writable: true, enumerable: true, configurable: true } as PropertyDescriptor)
  else object[key] = value
}

/**
 * Reads each of `keys` from the app's object exactly once, silencing promises, so a getter runs
 * once and can't return one value when checked and another when copied. `own`: own properties
 * only; otherwise also its own prototypes' (an error's `name`). Never Object.prototype's, and the
 * result has no prototype either, so a polluted Object.prototype can't supply a field.
 */
export function readFields(source: object, keys: Iterable<string>, own: boolean): Record<string, unknown> {
  const out = blank()
  for (const key of keys) {
    if (own ? !hasOwn(source, key) : !definedBelowObjectPrototype(source, key)) continue
    const value = (source as Record<string, unknown>)[key]
    silence(value)
    if (value !== undefined) out[key] = value
  }
  return out
}

/**
 * Reads the app's settings (options) once each, silencing promises. A setting may come from the
 * object or its own prototypes (a config class's getters, `Object.create(defaults)`), never from
 * Object.prototype, so a polluted prototype can't supply one. The result has no prototype either.
 */
export function readSettings(source: unknown, keys: Iterable<string>): Record<string, unknown> {
  if ((typeof source !== 'object' && typeof source !== 'function') || source === null) return blank()
  return readFields(source, keys, false)
}

/**
 * Whether the app's object has `key` itself or from its own prototypes (a class's getters), not only
 * from Object.prototype, which a polluted prototype could have given it. Asked as JavaScript asks
 * it, level by level (hasOwnProperty, then the prototype): a Proxy answers through its traps, so a
 * transparent one answers as its target would. Throws what a trap throws. Bounded: a Proxy's
 * getPrototypeOf can make the chain endless.
 */
export function definedBelowObjectPrototype(source: object, key: string): boolean {
  let object: object | null = source
  for (let level = 0; level < 32 && object !== null && object !== Object.prototype; level++) {
    if (hasOwn(object, key)) return true
    object = prototypeOf(object)
  }
  return false
}

/**
 * A native promise adopting what the app's code answered (the tool's own function, a `tools/list`
 * handler, the initialize result, `onError` and the logger's method), when it is a thenable;
 * undefined for anything else, which the caller takes as it is. Adoption is JavaScript's own, as
 * the code the SDK stands in for adopts that answer, and nothing of the app's runs before it:
 *
 * - By default as `await` adopts it (SDK 1.x's tool call, which runs `await Promise.resolve()`
 *   of the tool's answer, and the client's own callbacks): a native promise is a thenable by its
 *   internal slot, and is handed to exactly that, nothing read before. Promise.resolve() and
 *   `await` each read its `constructor`: one of this realm's Promise is followed as it is, an
 *   own `then` of its skipped; any other's (a subclass's, another realm's) `then` is read and run
 *   in a job.
 * - `resolving`, as a promise's resolve function adopts (SDK 2.x's tool call, whose executor is
 *   an async function returning the tool's answer, and both SDKs' request dispatch, which
 *   resolves with what a handler answered): a native promise is asked for its `then` as any other
 *   value is, so an own `then` of its runs too.
 *
 * A value asked for its `then` has it read once, to tell; a function, it is the one
 * Promise.resolve() calls, in a job of its own as JavaScript calls a thenable's, with JavaScript's
 * own resolving functions, so a value it resolves with is adopted in turn as JavaScript adopts it.
 * Only the second read of `then` that JavaScript would make is spared: the method checked is the
 * method called. What that read gives, and what the `then` returns, which JavaScript drops, are
 * silenced where that runs none of the app's code (see quiet()). Throws what reading `then`
 * throws; a native promise adopted as `await` adopts it rejects with what reading its
 * `constructor` throws, as `await` does.
 */
export function adopted(value: unknown, resolving = false): Promise<unknown> | undefined {
  if (isPromise(value) && !resolving) return awaitedResolved(value)
  if ((typeof value !== 'object' && typeof value !== 'function') || value === null) return undefined
  const then: unknown = (value as { then?: unknown }).then
  quiet(then)
  if (typeof then !== 'function') return undefined
  return Promise.resolve({ then: (onFulfilled: unknown, onRejected: unknown) => quiet(Reflect.apply(then, value, [onFulfilled, onRejected])) })
}

/** `await Promise.resolve(value)`, SDK 1.x's adoption of a tool's answer, run as it runs it: each reads a native promise's `constructor` once. */
async function awaitedResolved(value: unknown): Promise<unknown> {
  return await Promise.resolve(value)
}

/**
 * Follows `promise`, what Promise.resolve() gave for an answer of the app's code, as `await`
 * follows the promise it adopts: through Promise.prototype.then itself. Promise.resolve() hands a
 * native promise of this realm's Promise back as it is, and a method called on that would be the
 * app's own if it has one (an own `then`, or `finally`, which calls `then`), run by the SDK, at
 * once, where `await` never runs it. Any other promise is Promise.resolve()'s own.
 */
export function follow<T = unknown, U = never>(promise: Promise<unknown>, onFulfilled?: (value: unknown) => T | PromiseLike<T>, onRejected?: (reason: unknown) => U | PromiseLike<U>): Promise<T | U> {
  return Promise.prototype.then.call(promise, onFulfilled, onRejected) as Promise<T | U>
}

/**
 * Silences `value` only where that runs none of the app's code: a native promise of this realm's
 * Promise itself, without a `constructor` of its own. silence() reads a promise's `constructor`
 * and makes a promise of its species, which for a subclass runs the subclass's constructor, as
 * nothing else would. What adopted() reads, and what a `then` returns, JavaScript itself reads or
 * drops too: a promise there that bare JavaScript would leave unhandled is never left unhandled
 * more often, and never costs an app's constructor a run. Never a Proxy, which isPromise() never
 * takes for a promise, so asking for the prototype runs no trap.
 */
function quiet(value: unknown): void {
  if (isPromise(value) && Object.getPrototypeOf(value) === Promise.prototype && !hasOwn(value, 'constructor')) silence(value)
}

/**
 * The prototype of the app's object, silenced as it is read: a Proxy's getPrototypeOf is the app's
 * code, and may return a rejected promise, which nothing else would handle.
 */
function prototypeOf(value: object): object | null {
  const proto = Object.getPrototypeOf(value) as object | null
  silence(proto)
  return proto
}

/**
 * An object's own enumerable string keys, in for...in's order, listed once per `listed`, one shaping
 * pass's: a snapshot, so a key a getter adds later in the pass isn't seen there. JavaScript lists
 * every key of an object before its first, its hidden ones (not enumerable, symbols) included, so a
 * listing costs the object's whole key table, outside the scan budget: the same object, referred to
 * from every item of a list, would cost that at every item.
 */
export function listKeys(value: object, listed: Map<object, readonly string[]>): readonly string[] {
  let keys = listed.get(value)
  if (keys === undefined) listed.set(value, (keys = Object.keys(value)))
  return keys
}

export function isPlainObject(value: unknown): value is Record<string, unknown> {
  if (typeof value !== 'object' || value === null) return false
  const proto = prototypeOf(value)
  return proto === Object.prototype || proto === null
}

/**
 * Converts a value to JSON without throwing. Parts JSON can't encode (BigInt, functions, Map,
 * class instances, circular references, `undefined` in arrays) are removed or replaced, and the
 * result is marked lossy. A value that is itself not encodable fails. `toJSON` methods run as in
 * JSON.stringify, also on what one returns; one that returns its own object reads it as it is, and
 * one only Object.prototype has never runs.
 */
export function toJson(value: unknown, budget: Budget = { remaining: Number.POSITIVE_INFINITY }): JsonResult {
  const state: State = { lossy: false, budget, tooLarge: false }
  const result = convert(value, new Set(), 0, state)
  if (state.tooLarge) return { ok: false, tooLarge: true }
  return result === FAIL ? { ok: false, tooLarge: false } : { ok: true, value: result, lossy: state.lossy }
}

const FAIL = Symbol('fail')

interface State {
  lossy: boolean
  budget: Budget
  tooLarge: boolean
}

function spend(state: State, cost: number): boolean {
  state.budget.remaining -= cost
  if (state.budget.remaining < 0) state.tooLarge = true
  return !state.tooLarge
}

function convert(value: unknown, ancestors: Set<object>, depth: number, state: State): Json | typeof FAIL {
  // First, before anything can return: every value read from the app's data passes through here.
  silence(value)
  if (state.tooLarge || !spend(state, 1)) return FAIL
  if (value === null) return null
  switch (typeof value) {
    case 'string':
      return spend(state, value.length) ? value : FAIL
    case 'boolean':
      return value
    case 'number':
      return Number.isFinite(value) ? value : FAIL
    case 'object':
      break
    default:
      return FAIL
  }
  if (depth >= MAX_DEPTH || ancestors.has(value as object)) return FAIL
  // Binary data isn't JSON. JSON.stringify would run Buffer's toJSON, which copies every byte into
  // an array (8 times the Buffer's size) before any budget could stop it.
  if (ArrayBuffer.isView(value)) return FAIL

  // Never a toJSON only Object.prototype has: a polluted one must not replace the app's objects.
  const toJSON = definedBelowObjectPrototype(value as object, 'toJSON') ? (value as { toJSON?: unknown }).toJSON : undefined
  silence(toJSON)
  /** A toJSON that returned its own object, which is then read as it is, as JSON.stringify reads it. */
  let itself = false
  if (typeof toJSON === 'function') {
    let replaced: unknown
    try {
      replaced = toJSON.call(value)
    } catch {
      return FAIL
    }
    if (replaced === value) {
      itself = true
    } else {
      ancestors.add(value as object)
      const result = convert(replaced, ancestors, depth + 1, state)
      ancestors.delete(value as object)
      return result
    }
  }

  if (Array.isArray(value)) {
    ancestors.add(value)
    const out: Json[] = []
    // Index loop, charging every slot: holes and undefined items cost work too.
    for (let i = 0; i < value.length; i++) {
      if (!spend(state, 1)) break
      const item: unknown = value[i]
      const converted = item === undefined ? FAIL : convert(item, ancestors, depth + 1, state)
      if (state.tooLarge) break
      if (converted === FAIL) {
        state.lossy = true
        out.push(null)
      } else {
        out.push(converted)
      }
    }
    ancestors.delete(value)
    return state.tooLarge ? FAIL : out
  }

  if (!isPlainObject(value)) return FAIL

  ancestors.add(value)
  const out: { [key: string]: Json } = {}
  // Its own keys, never `for...in`, which would ask a Proxy for its prototype again, after
  // isPlainObject(), and JavaScript consumes that answer itself (a promise there stays unhandled).
  // Checked again as each is read: a getter may have removed a later one.
  for (const key of Object.keys(value)) {
    if (!hasOwn(value, key)) continue
    // How the object is written, not part of it: not read again.
    if (itself && key === 'toJSON') continue
    const item = value[key]
    // Before the budget check or the __proto__ skip can leave without converting it.
    silence(item)
    if (!spend(state, item === undefined ? 1 : key.length + 1)) break
    if (item === undefined) continue
    if (key === '__proto__') {
      state.lossy = true
      continue
    }
    const converted = convert(item, ancestors, depth + 1, state)
    if (state.tooLarge) break
    if (converted === FAIL) state.lossy = true
    else out[key] = converted
  }
  ancestors.delete(value)
  return state.tooLarge ? FAIL : out
}

/** Code points, as Python counts characters: an astral character is one, a lone surrogate one. */
function codePoints(text: string): number {
  let count = text.length
  for (let i = 0; i < text.length - 1; i++) {
    const unit = text.charCodeAt(i)
    if (unit >= 0xd800 && unit <= 0xdbff && (text.charCodeAt(i + 1) & 0xfc00) === 0xdc00) {
      count--
      i++
    }
  }
  return count
}

/**
 * Exact length of a value's JSON.stringify text, in code points, measured without keeping a copy and without running its
 * `toJSON`, within a shared work allowance. Says why when it can't: the allowance ran out, or the
 * value isn't plain JSON. `listed`: the keys of the objects already listed in this pass (`listKeys`),
 * so an object the tool results of one event share is listed once.
 */
export function jsonLength(value: unknown, allowance: Budget, listed: Map<object, readonly string[]> = new Map()): number | 'too_large' | 'not_json' {
  const ancestors = new Set<object>()
  const charge = (cost: number) => (allowance.remaining -= cost) >= 0
  const skipped = (item: unknown) => item === undefined || typeof item === 'function' || typeof item === 'symbol'

  const measure = (item: unknown, depth: number): number | undefined => {
    silence(item)
    if (!charge(1) || depth >= MAX_DEPTH) return undefined
    if (item === null) return 4
    switch (typeof item) {
      case 'string':
        return charge(item.length) ? codePoints(JSON.stringify(item)) : undefined
      case 'boolean':
        return item ? 4 : 5
      case 'number':
        // Same as JSON.stringify for finite numbers (including -0 and exponents), and faster.
        return Number.isFinite(item) ? String(item).length : 4
      case 'object':
        break
      default:
        return undefined
    }
    if (ancestors.has(item as object)) return undefined
    if (Array.isArray(item)) {
      ancestors.add(item)
      let total = 2 + Math.max(0, item.length - 1)
      for (let i = 0; i < item.length; i++) {
        const element: unknown = item[i]
        // JSON.stringify writes null for undefined, functions and symbols in arrays.
        const length = skipped(element) ? (charge(1) ? 4 : undefined) : measure(element, depth + 1)
        if (length === undefined) return undefined
        total += length
      }
      ancestors.delete(item)
      return total
    }
    if (!isPlainObject(item)) return undefined
    ancestors.add(item)
    let total = 2
    let first = true
    // Checked again as each is read: a getter may have removed a later one.
    for (const key of listKeys(item, listed)) {
      if (!hasOwn(item, key)) continue
      const element = item[key]
      silence(element)
      // ...and omits them in objects.
      if (skipped(element)) {
        if (!charge(1)) return undefined
        continue
      }
      if (!charge(key.length)) return undefined
      const length = measure(element, depth + 1)
      if (length === undefined) return undefined
      total += (first ? 0 : 1) + codePoints(JSON.stringify(key)) + 1 + length
      first = false
    }
    ancestors.delete(item)
    return total
  }

  const length = measure(value, 0)
  if (allowance.remaining < 0) return 'too_large'
  return length ?? 'not_json'
}
