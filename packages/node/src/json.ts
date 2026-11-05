import { types } from 'node:util'
import type { Json } from './types.js'

const MAX_DEPTH = 64
/** The longest an array can be: 2^32 - 1. */
const MAX_ARRAY_LENGTH = 0xffffffff
const { isPromise } = types
// As they were when the SDK loaded: an app patching them later doesn't change what silence() calls.
// The engine's own then, from a promise an async function returns: always a native one, even when
// the app replaced the global Promise first (with bluebird's, or zone.js), whose then can't attach a
// handler to the native promises async functions and Node return.
const promiseThen = Object.getPrototypeOf((async () => {})()).then as typeof Promise.prototype.then
const { apply } = Reflect
const ignore = () => {}
export const hasOwn = (object: object, key: string) => Object.prototype.hasOwnProperty.call(object, key)

/**
 * Work allowance shared by everything one event converts. Every array slot, key and node is
 * charged before it is visited. For ordinary data the charge is a lower bound on the JSON size, so
 * running out means the event can't fit. Not charged: listing an object's keys, which JavaScript
 * does in full, hidden keys included, before the first is visited, and code the app's data runs
 * (getters, `toJSON`, Proxy traps). Tool results, never sent, are measured against an allowance of
 * their own (RESULT_MEASURE_LIMIT in normalize.ts), not the event's.
 */
export interface Budget {
  remaining: number
}

export type JsonResult = { ok: true; value: Json; lossy: boolean } | { ok: false; tooLarge: boolean }

/**
 * Gives a promise the SDK got from the app's code (a getter, a `toJSON`, a Proxy trap whose result
 * the SDK reads) a rejection handler. If it rejected with nobody listening, Node would crash the
 * app's process. The SDK never uses it: a promise isn't JSON.
 *
 * Telling a promise runs none of the app's code: `isPromise` reads the internal slot, so it finds a
 * native promise from any realm (a `vm` context's, or Node's own seen from a test runner's) and
 * whatever its prototype, with no Proxy trap. Attaching the handler is the engine's own `then`,
 * whatever the global Promise is, which reads the promise's `constructor` and its `Symbol.species`:
 * the app's code, for a subclass or a redefined `constructor`. If that throws or gives no
 * constructor, no handler can be attached, and the promise is left as it is. No other thenable's
 * `then` is called: it may do work.
 */
export function silence(value: unknown): void {
  try {
    if (isPromise(value)) apply(promiseThen, value, [undefined, ignore])
  } catch {
    // The promise's `constructor` or `Symbol.species` threw, or isn't a constructor.
  }
}

/**
 * Stands for a value the app's code wouldn't let the SDK read: a getter or a Proxy trap threw, or
 * the Proxy was revoked (a leftover Immer draft, say). A symbol: neither JSON nor any field's type,
 * so the checks that follow treat it as a value of the wrong type, and remove it as they would one.
 */
const UNREADABLE = Symbol('unreadable')

/** Reads one property or slot of the app's object, silencing a promise. A read that throws gives UNREADABLE. */
export function read(source: object, key: string | number): unknown {
  try {
    const value = (source as Record<string | number, unknown>)[key]
    silence(value)
    return value
  } catch {
    return UNREADABLE
  }
}

/**
 * An array's `length`, read once and silenced: an Array Proxy's is the app's code, and may give a
 * rejected promise, or anything else. Undefined unless it is a length an array can have, an
 * integer from 0 to 2^32 - 1.
 */
export function arrayLength(value: readonly unknown[]): number | undefined {
  const length = read(value, 'length')
  return typeof length === 'number' && Number.isInteger(length) && length >= 0 && length <= MAX_ARRAY_LENGTH ? length : undefined
}

/**
 * Reads each of `keys` from the app's object exactly once, silencing promises, so a getter runs
 * once and can't return one value when checked and another when copied. `own`: own properties
 * only; otherwise also its own prototypes' (an error class's `name`). Never Object.prototype's, and
 * the result has no prototype either, so a polluted Object.prototype can't supply a field. A field
 * that can't be read (a getter or a Proxy trap throws) is UNREADABLE.
 */
export function readFields(source: object, keys: Iterable<string>, own: boolean): Record<string, unknown> {
  const out: Record<string, unknown> = Object.create(null)
  for (const key of keys) {
    let value: unknown
    try {
      if (own ? !hasOwn(source, key) : !definedBelowObjectPrototype(source, key)) continue
      value = (source as Record<string, unknown>)[key]
    } catch {
      value = UNREADABLE
    }
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
  const out: Record<string, unknown> = Object.create(null)
  if ((typeof source !== 'object' && typeof source !== 'function') || source === null) return out
  for (const key of keys) {
    if (!definedBelowObjectPrototype(source, key)) continue
    const value = (source as Record<string, unknown>)[key]
    silence(value)
    if (value !== undefined) out[key] = value
  }
  return out
}

/**
 * Whether `key` is the object's own or its own prototypes' (a class's getters), not only
 * Object.prototype's, which a polluted prototype could have given it. Each prototype is silenced as
 * it is read: a Proxy's getPrototypeOf is the app's code, and may answer with a rejected promise.
 * Bounded: a Proxy's getPrototypeOf can make the chain endless.
 */
function definedBelowObjectPrototype(source: object, key: string): boolean {
  let object: object | null = source
  for (let level = 0; level < 32 && object !== null && object !== Object.prototype; level++) {
    if (hasOwn(object, key)) return true
    object = Object.getPrototypeOf(object) as object | null
    silence(object)
  }
  return false
}

/**
 * Whether its prototype is Object.prototype or null. Never throws: an object whose prototype can't
 * be read (a revoked Proxy, a trap that throws) isn't plain. The prototype is silenced: a Proxy's
 * getPrototypeOf may answer with a rejected promise.
 */
export function isPlainObject(value: unknown): value is Record<string, unknown> {
  if (typeof value !== 'object' || value === null) return false
  let proto: unknown
  try {
    proto = Object.getPrototypeOf(value)
  } catch {
    return false
  }
  silence(proto)
  return proto === Object.prototype || proto === null
}

/**
 * Converts a value to JSON without throwing. Parts JSON can't encode (BigInt, functions, Map,
 * class instances, circular references, `undefined` in arrays) are removed or replaced, and the
 * result is marked lossy, as are parts the app's code won't let it read (a getter or Proxy trap
 * that throws, a revoked Proxy). A value that is itself not encodable, or can't be read, fails.
 * `toJSON` methods run, like in JSON.stringify.
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

  ancestors.add(value as object)
  try {
    return convertObject(value as object, ancestors, depth, state)
  } catch {
    // The app's code wouldn't let it be read (a `toJSON` getter, a trap listing or checking its
    // keys, a revoked Proxy): not JSON, as JSON.stringify would have thrown.
    return FAIL
  } finally {
    ancestors.delete(value as object)
  }
}

/** An object's part of convert(), in its try: anything the app's code throws here fails the value. */
function convertObject(value: object, ancestors: Set<object>, depth: number, state: State): Json | typeof FAIL {
  const toJSON = (value as { toJSON?: unknown }).toJSON
  silence(toJSON)
  if (typeof toJSON === 'function') {
    let replaced: unknown
    try {
      replaced = toJSON.call(value)
    } catch {
      return FAIL
    }
    if (replaced === value) return FAIL
    return convert(replaced, ancestors, depth + 1, state)
  }

  if (Array.isArray(value)) {
    // Read once: an Array Proxy's length is the app's code.
    const length = arrayLength(value)
    if (length === undefined) return FAIL
    const out: Json[] = []
    // Index loop, charging every slot: holes and undefined items cost work too.
    for (let i = 0; i < length; i++) {
      if (!spend(state, 1)) break
      const item = read(value, i)
      const converted = item === undefined ? FAIL : convert(item, ancestors, depth + 1, state)
      if (state.tooLarge) break
      if (converted === FAIL) {
        state.lossy = true
        out.push(null)
      } else {
        out.push(converted)
      }
    }
    return state.tooLarge ? FAIL : out
  }

  if (!isPlainObject(value)) return FAIL

  const out: { [key: string]: Json } = {}
  // Own keys only: for...in would also walk the prototype chain, through a Proxy's getPrototypeOf
  // trap, whose answers never reach the SDK. Each is checked again as it is read: a getter may have
  // removed a later one.
  for (const key of Object.keys(value)) {
    if (!hasOwn(value, key)) continue
    // Read and silenced before the budget check or the __proto__ skip can leave without converting it.
    const item = read(value, key)
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
  return state.tooLarge ? FAIL : out
}

/**
 * Exact JSON.stringify length of a value, measured without keeping a copy and without running its
 * `toJSON`, within a shared work allowance. Says why when it can't: the allowance ran out, or the
 * value isn't plain JSON, or the app's code won't let it be read (a getter or Proxy trap that
 * throws, a revoked Proxy), which JSON.stringify would have thrown on.
 */
export function jsonLength(value: unknown, allowance: Budget): number | 'too_large' | 'not_json' {
  const ancestors = new Set<object>()
  const charge = (cost: number) => (allowance.remaining -= cost) >= 0
  const skipped = (item: unknown) => item === undefined || typeof item === 'function' || typeof item === 'symbol'

  const measure = (item: unknown, depth: number): number | undefined => {
    silence(item)
    if (!charge(1) || depth >= MAX_DEPTH) return undefined
    if (item === null) return 4
    switch (typeof item) {
      case 'string':
        return charge(item.length) ? JSON.stringify(item).length : undefined
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
      // Read once: an Array Proxy's length is the app's code.
      const count = arrayLength(item)
      if (count === undefined) return undefined
      ancestors.add(item)
      let total = 2 + Math.max(0, count - 1)
      for (let i = 0; i < count; i++) {
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
    // Own keys only, each checked again as it is read, as in convert().
    for (const key of Object.keys(item)) {
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
      total += (first ? 0 : 1) + JSON.stringify(key).length + 1 + length
      first = false
    }
    ancestors.delete(item)
    return total
  }

  let length: number | undefined
  try {
    length = measure(value, 0)
  } catch {
    // The app's code wouldn't let it be read (a getter or Proxy trap that throws, a revoked Proxy).
    length = undefined
  }
  if (allowance.remaining < 0) return 'too_large'
  return length ?? 'not_json'
}
