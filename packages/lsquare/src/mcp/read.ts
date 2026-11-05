// Reading the app's values without ever throwing, or leaving a rejected promise unhandled.
// The core's reading helpers are stateless: the bundle gets its own copy of them, sharing nothing.
import { adopted as adopting, definedBelowObjectPrototype, follow, put, silence } from '../json.js'

/**
 * The core's: a native promise, from any realm, gets a rejection handler, and no other thenable's
 * `then` is called. Reading a value must not run the app's code (a query builder runs its query).
 */
export { follow, silence }

/**
 * What a callback the wrapper waits for (the tool's own function, a `tools/list` handler, the
 * initialize result) answered, adopted as the SDK it stands in for adopts it, when it is a
 * thenable: the core's adopted(), as `await` adopts it, or, `resolving`, as a resolve function
 * does (see there). Undefined for anything else, which the wrapper hands on as it is. A `then`
 * getter that throws gives a promise rejected with what it threw, as `await` and a resolve
 * function give one, having read it once too.
 */
export function adopted(value: unknown, resolving = false): Promise<unknown> | undefined {
  try {
    return adopting(value, resolving)
  } catch (error) {
    return Promise.reject(error)
  }
}

/** One property of the app's value, read without throwing. */
export function read(value: unknown, key: string | symbol): unknown {
  try {
    if ((typeof value !== 'object' && typeof value !== 'function') || value === null) return undefined
    const item = (value as Record<string | symbol, unknown>)[key]
    silence(item)
    return item
  } catch {
    return undefined
  }
}

/**
 * One field the app's object declares (an option, an extra, a field of what `identify` returned):
 * its own, or from its own prototypes (a class's getters, `Object.create(defaults)`), never
 * Object.prototype's, so a polluted prototype can't supply one. Read without throwing.
 */
export function declared(value: unknown, key: string): unknown {
  try {
    if ((typeof value !== 'object' && typeof value !== 'function') || value === null) return undefined
    return definedBelowObjectPrototype(value, key) ? read(value, key) : undefined
  } catch {
    return undefined
  }
}

/** An own property of the app's object, never one it inherits, such as `constructor`. */
export function own(value: unknown, key: string): unknown {
  try {
    if ((typeof value !== 'object' && typeof value !== 'function') || value === null) return undefined
    return Object.prototype.hasOwnProperty.call(value, key) ? read(value, key) : undefined
  } catch {
    return undefined
  }
}

/**
 * A copy of the app's object as JSON writes it, for a reply the wrapper changes: its own
 * enumerable string keys, in their order, each value read once and silenced. A key in `given`
 * takes the value given, and comes last if the object lacks it, as a spread followed by it would
 * have it; one in `known` the value the wrapper already read of it, which isn't read again, so
 * what was checked is what goes out, and one the object doesn't list stays out. Not a spread: a
 * spread also reads the object's symbol keys, which no JSON writes, so a promise one gave would
 * be the wrapper's alone.
 */
export function copied(value: object, given: Record<string, unknown> = {}, known: Record<string, unknown> = {}): Record<string, unknown> {
  const copy: Record<string, unknown> = {}
  // Added as a literal adds it (see put()): it goes to the client, through the MCP SDK.
  const set = (key: string, item: unknown) => put(copy, key, item)
  const has = (object: object, key: string) => Object.prototype.hasOwnProperty.call(object, key)
  for (const key of Object.keys(value)) {
    if (has(given, key)) {
      set(key, given[key])
    } else if (has(known, key)) {
      set(key, known[key])
    } else {
      const item = (value as Record<string, unknown>)[key]
      silence(item)
      set(key, item)
    }
  }
  for (const key of Object.keys(given)) if (!has(copy, key)) set(key, given[key])
  return copy
}
