import { blank, definedBelowObjectPrototype, hasOwn, isPlainObject, listKeys, put, silence } from './json.js'
import type { Json } from './types.js'

/**
 * Shortening (spec section 2): turns values from the app into event JSON within the limits, before
 * redaction. Strings over a field's limit become `Cut`s, which keep what redaction needs to see past
 * the cut; binary-looking strings become `Binary`. Redaction (redact.ts) turns both into JSON.
 *
 * Every count is in Unicode code points, as Python counts, so both SDKs cut in the same places.
 */

/** Characters (code points) read per event, plus one per value visited, across all its fields. */
export const SCAN_BUDGET = 1_000_000
export const LIMITS = { input: 16_000, agent: 16_000, result: 4_000, intent: 500 } as const
export const MAX_ENTRIES = 100
export const MAX_LEVELS = 10
/** Redaction reads this far past a cut, so a secret that runs into it is still recognised. */
export const LOOKAHEAD = 8_000
const BINARY_MIN = 1_000
const BINARY_HEAD = 4_096
const BINARY_TAIL = 1_024
const DATA_HEADER_MAX = 1_024
export const CUT_MARK = '…[cut]'
/** A marker this SDK writes, at the end of a string it cut. */
const MARKER = /…\[(?:\+(\d{1,16}) chars|cut)\]$/
/** A placeholder from redaction, kept as it is under a secret key name. */
const WHOLE_PLACEHOLDER = /^\[(?:EMAIL|PHONE|CARD|SECRET|IP)_\d{1,9}\]$/

export interface Scan {
  remaining: number
}

/**
 * A shortened string: `window` is what was kept plus what redaction reads past `cut` (a UTF-16
 * index), cut for a field whose limit is `limit`.
 */
export class Cut {
  constructor(
    readonly window: string,
    readonly cut: number,
    readonly mark: string,
    readonly limit: number,
    /** Already read for values under secret key names, with the lookahead this one no longer has. */
    readonly scanned: boolean = false,
  ) {}
}

/** A string that held a file: only its decoded size is kept. */
export class Binary {
  constructor(readonly bytes: number) {}
}

/** A container below the nesting limit. */
export class TooDeep {}

/**
 * A value under a secret key name, replaced whole before anything else sees it, `beforeSend`
 * included. `identity` tells two equal secrets apart from different ones, for numbering: the raw
 * value, or undefined when it couldn't be read whole, so it is numbered on its own.
 */
export class Secret {
  constructor(readonly identity: string | undefined) {}
}

export type Shaped = string | number | boolean | null | Cut | Binary | TooDeep | Secret | Shaped[] | { [key: string]: Shaped }

export interface ShapeState {
  scan: Scan
  /** Something was cut, left out at a limit, or replaced by a size. */
  shortened: boolean
  /** Something couldn't be encoded as JSON and was removed (0.1's `not_json`). */
  lossy: boolean
  /** Shaping `beforeSend`'s output: a string this SDK cut keeps its marker. */
  afterHook: boolean
  /** The strings this SDK cut or rewrote before `beforeSend`, as the hook got them (`Redactor.cuts`). */
  cuts: ReadonlyMap<string, number> | undefined
  /** This event's secret key names; undefined when the `secret` pattern is off. */
  secretKeys: ((key: string) => boolean) | undefined
  /** The same, while shaping a field whose keys come from the app's data; otherwise undefined. */
  secretKey: ((key: string) => boolean) | undefined
  /** The keys of the objects this pass listed, each once (`ownKeys`), shared with measuring tool results. */
  listed: Map<object, readonly string[]> | undefined
}

export function newState(
  options: { afterHook?: boolean; budget?: number; cuts?: ReadonlyMap<string, number>; secretKeys?: ((key: string) => boolean) | undefined } = {},
): ShapeState {
  // Own fields only: one left out never comes from a polluted Object.prototype.
  const { budget, afterHook, cuts, secretKeys } = blank(options)
  return {
    scan: { remaining: budget ?? SCAN_BUDGET },
    shortened: false,
    lossy: false,
    afterHook: afterHook ?? false,
    cuts,
    secretKeys,
    secretKey: undefined,
    listed: undefined,
  }
}

/**
 * An object's own keys, in for...in's order, listed once per pass however often the app's data
 * refers to it (`listKeys`): a snapshot, so a key a getter adds to it later in the pass isn't seen.
 */
export function ownKeys(value: object, state: ShapeState): readonly string[] {
  return listKeys(value, (state.listed ??= new Map()))
}

/** Returned for values that aren't JSON at all. A symbol, so no input can be mistaken for it. */
export const NOT_JSON = Symbol('not_json')

/**
 * Advances `count` code points from UTF-16 index `from`. Returns the index reached and how many code
 * points were passed. A lone surrogate counts as one, as in Python.
 */
export function advance(text: string, from: number, count: number): [index: number, passed: number] {
  let index = from
  let passed = 0
  while (passed < count && index < text.length) {
    const unit = text.charCodeAt(index)
    const pair = unit >= 0xd800 && unit <= 0xdbff && index + 1 < text.length && (text.charCodeAt(index + 1) & 0xfc00) === 0xdc00
    index += pair ? 2 : 1
    passed++
  }
  return [index, passed]
}

const BASE64_CHAR = /^[A-Za-z0-9+/\-_=]*$/

/** Bytes a base64 payload of `length` characters decodes to; `text` ends with its padding (at most 2). */
export function decodedSize(length: number, text: string): number {
  let padding = 0
  while (padding < 2 && padding < length && text.charCodeAt(text.length - 1 - padding) === 61 /* = */) padding++
  return Math.floor(((length - padding) * 3) / 4)
}

/**
 * The decoded size of a string that holds a file: any `data:` URI with `;base64,`, or a string of
 * at least 1,000 characters whose first 4 KB and last 1 KB are only base64 characters. A heuristic:
 * the rest isn't read. Bounded work, not charged to the scan budget.
 */
export function binarySize(text: string): number | undefined {
  if (text.length >= 5 && text.slice(0, 5).toLowerCase() === 'data:') {
    const comma = text.slice(0, DATA_HEADER_MAX + 1).indexOf(',', 5)
    if (comma !== -1 && text.slice(5, comma).toLowerCase().endsWith(';base64')) {
      return decodedSize(text.length - comma - 1, text)
    }
  }
  // Fewer than 1,000 UTF-16 units means fewer than 1,000 code points. More units but fewer code
  // points means astral characters, which aren't base64, so the checks below say no.
  if (text.length < BINARY_MIN) return undefined
  if (!BASE64_CHAR.test(text.slice(0, BINARY_HEAD))) return undefined
  if (text.length > BINARY_HEAD && !BASE64_CHAR.test(text.slice(-BINARY_TAIL))) return undefined
  return decodedSize(text.length, text)
}

/**
 * Shortens one string to `limit` code points. Within the limit it stays a string (charged its
 * length); past it, a `Cut` keeping the first `limit` code points plus `LOOKAHEAD` more for
 * redaction, marked `…[+N chars]` with the exact count of the rest when that fits in the budget,
 * which is charged for counting it. When it doesn't fit, counting it would use the budget up, so it
 * is: the string ends `…[cut]`, and so does every string after it. Once the budget runs out,
 * strings are cut where it ran out.
 */
export function shapeString(text: string, limit: number, state: ShapeState): string | Cut | Binary {
  const { scan } = state
  if (text === '') return text
  if (scan.remaining <= 0) {
    state.shortened = true
    return new Cut('', 0, CUT_MARK, limit)
  }
  const bytes = binarySize(text)
  if (bytes !== undefined) {
    state.shortened = true
    return new Binary(bytes)
  }

  // After beforeSend: a string this SDK cut, as the hook got it, is kept as it is where its limit
  // is no tighter than the one it was cut at. Redaction may have made it longer than that limit
  // (`a@b.co` is shorter than `[EMAIL_1]`). Any other marked string is shortened as usual, adding
  // the count in its marker to what is cut now.
  let earlier: string | undefined
  if (state.afterHook) {
    // Only the end is searched: a regex anchored at the end would still scan a long string.
    const tail = text.slice(-32)
    const marked = MARKER.exec(tail)
    if (marked) {
      const body = text.slice(0, text.length - tail.length + marked.index)
      const cutAt = state.cuts?.get(text)
      if (cutAt !== undefined && cutAt <= limit) {
        state.shortened = true
        const [end, passed] = advance(body, 0, scan.remaining)
        if (end === body.length) {
          scan.remaining -= passed
          return new Cut(body, body.length, marked[0], limit, true)
        }
        scan.remaining = 0
        return new Cut(body.slice(0, end), end, CUT_MARK, limit, true)
      }
      earlier = marked[0]
      text = body
    } else {
      // A string whose secret values were replaced before the hook, as the hook got it, is kept
      // whole where its length before that fits: placeholders may have made it longer than that.
      const before = state.cuts?.get(text)
      if (before !== undefined && before <= limit && before <= scan.remaining) {
        scan.remaining -= before
        return text
      }
    }
  }

  const readable = Math.min(limit, scan.remaining)
  const [cut, kept] = advance(text, 0, readable)
  if (cut === text.length) {
    scan.remaining -= kept
    return earlier === undefined ? text : new Cut(text, cut, earlier, limit)
  }
  state.shortened = true
  // Redaction reads LOOKAHEAD past every cut, also where the budget runs out, so a token running
  // into the cut is matched whole. The budget runs out once, so that reads at most LOOKAHEAD more.
  const [end, ahead] = advance(text, cut, LOOKAHEAD)
  if (kept < limit || kept + ahead > scan.remaining) {
    // The budget ran out inside this string, or inside what redaction reads past the cut.
    scan.remaining = 0
    return new Cut(text.slice(0, end), cut, CUT_MARK, limit)
  }
  scan.remaining -= kept + ahead
  let rest: number | undefined = ahead
  if (end < text.length) {
    const [last, counted] = advance(text, end, scan.remaining + 1)
    if (last === text.length && counted <= scan.remaining) {
      scan.remaining -= counted
      rest += counted
    } else {
      scan.remaining = 0
      rest = undefined
    }
  }
  return new Cut(text.slice(0, end), cut, combine(rest, earlier), limit)
}

/** Whether a string ends with a marker this SDK writes. */
export function isMarked(text: string): boolean {
  return MARKER.test(text.slice(-32))
}

/** The marker for `rest` characters cut now (undefined: not counted), plus a marker from before. */
function combine(rest: number | undefined, earlier: string | undefined): string {
  if (rest === undefined) return CUT_MARK
  if (earlier === undefined) return `…[+${rest} chars]`
  const before = MARKER.exec(earlier)?.[1]
  return before === undefined ? CUT_MARK : `…[+${rest + Number(before)} chars]`
}

/** Shaped JSON as plain JSON, without redaction: a value's identity under a secret key, and output with redaction off. */
export function plain(value: Shaped): Json {
  if (value instanceof Cut) return value.window.slice(0, value.cut) + value.mark
  if (value instanceof Binary) return { omitted: 'binary', bytes: value.bytes }
  if (value instanceof TooDeep) return { omitted: 'depth' }
  if (value instanceof Secret) return '[SECRET]'
  if (Array.isArray(value)) return value.map(plain)
  if (typeof value === 'object' && value !== null) {
    const out: { [key: string]: Json } = {}
    for (const [key, item] of Object.entries(value)) put(out, key, plain(item))
    return out
  }
  return value
}

/**
 * A value under a secret key name, read within the limits but never shortened: its identity is
 * the raw value. A string is read up to the field's limit; anything else is converted, and counts
 * only if nothing in it was cut, left out or turned into a size. Values that can't hold a secret
 * (null, booleans, values empty as given) and placeholders stay as they are.
 */
function secretValue(value: unknown, limit: number, state: ShapeState, ancestors: Set<object>, level: number): Shaped | typeof NOT_JSON {
  if (value === null || typeof value === 'boolean' || value === '') return value
  if (typeof value === 'string') return secretString(value, limit, state)
  const { shortened, lossy, secretKey } = state
  state.shortened = false
  state.lossy = false
  // Read as a whole, as 0.1 did: secret key names inside it don't matter.
  state.secretKey = undefined
  const converted = convert(value, limit, state, ancestors, level)
  const exact = !state.shortened && !state.lossy
  Object.assign(state, { shortened, lossy, secretKey })
  if (converted === NOT_JSON) return NOT_JSON
  const container = Array.isArray(converted) || (typeof converted === 'object' && converted !== null && Object.getPrototypeOf(converted) === Object.prototype)
  // Empty as it was given, not made empty by the budget or by leaving out what JSON can't hold.
  if (exact && container && Object.keys(converted as object).length === 0) return converted
  return new Secret(exact ? JSON.stringify(plain(converted)) : undefined)
}

/** A string under a secret key name: its raw value, read up to `limit`, or a number of its own. */
export function secretString(value: string, limit: number, state: ShapeState): Shaped {
  if (value === '' || (value.length <= 20 && WHOLE_PLACEHOLDER.test(value))) return value
  const [end, passed] = advance(value, 0, Math.min(limit, state.scan.remaining))
  state.scan.remaining -= passed
  return new Secret(end === value.length ? value : undefined)
}

/**
 * Converts a value from the app into shaped JSON, without throwing and without reading more than
 * the limits allow: lists and objects keep their first 100 entries, nesting stops at 10 levels, and
 * strings are shortened to `limit`. Objects are read in JavaScript's order (integer-like keys
 * first, ascending, then the others as they were added), which Python follows. Under a secret key
 * name, when `state.secretKey` is set, a value becomes a `Secret`. Parts JSON can't encode (BigInt, functions, Map, class
 * instances, circular references, binary buffers) are removed and the state marked lossy; a value
 * that is itself not encodable gives NOT_JSON. `toJSON` methods run as in JSON.stringify, also on
 * what one returns (so `toJSON() { return this.date }` gives the date's string, where JSON.stringify
 * gives `{}`); one that returns its own object reads it as it is, and one only Object.prototype has
 * never runs. Every value read from the app is read once, and its promises silenced.
 */
export function shapeJson(value: unknown, limit: number, state: ShapeState): Shaped | typeof NOT_JSON {
  return convert(value, limit, state, new Set(), 0)
}

/** Charges `cost` to the scan budget. Past it, false: the budget is used up, and the value marked shortened. */
export function spend(state: ShapeState, cost: number): boolean {
  if (state.scan.remaining < cost) {
    state.scan.remaining = 0
    state.shortened = true
    return false
  }
  state.scan.remaining -= cost
  return true
}

function convert(value: unknown, limit: number, state: ShapeState, ancestors: Set<object>, level: number): Shaped | typeof NOT_JSON {
  // First, before anything can return: every value read from the app's data passes through here.
  silence(value)
  if (value === null) return null
  switch (typeof value) {
    case 'string':
      return shapeString(value, limit, state)
    case 'boolean':
      return value
    case 'number':
      return Number.isFinite(value) ? value : NOT_JSON
    case 'object':
      break
    default:
      return NOT_JSON
  }
  if (ancestors.has(value as object)) return NOT_JSON
  // Binary data isn't JSON. JSON.stringify would run Buffer's toJSON, which copies every byte.
  if (ArrayBuffer.isView(value)) return NOT_JSON

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
      return NOT_JSON
    }
    if (replaced === value) {
      itself = true
    } else {
      ancestors.add(value as object)
      const result = convert(replaced, limit, state, ancestors, level)
      ancestors.delete(value as object)
      return result
    }
  }

  const container = Array.isArray(value) || isPlainObject(value)
  if (!container) return NOT_JSON
  if (level >= MAX_LEVELS) {
    state.shortened = true
    return new TooDeep()
  }

  ancestors.add(value)
  try {
    if (Array.isArray(value)) {
      const out: Shaped[] = []
      if (value.length > MAX_ENTRIES) state.shortened = true
      const end = Math.min(value.length, MAX_ENTRIES)
      // Index loop: holes and undefined items cost a visit too, and become null, as in JSON.
      for (let index = 0; index < end; index++) {
        if (!spend(state, 1)) break
        const item: unknown = value[index]
        const converted = item === undefined ? NOT_JSON : convert(item, limit, state, ancestors, level + 1)
        if (converted === NOT_JSON) {
          state.lossy = true
          out.push(null)
        } else {
          out.push(converted)
        }
      }
      return out
    }

    const out: { [key: string]: Shaped } = {}
    let kept = 0
    // Own keys only: a polluted prototype can't add entries. Checked again as each is read, since a
    // getter may have removed a later one.
    for (const key of ownKeys(value, state)) {
      if (!hasOwn(value, key)) continue
      // How the object is written, not part of it: not read again.
      if (itself && key === 'toJSON') continue
      if (kept >= MAX_ENTRIES) {
        state.shortened = true
        break
      }
      // A key is read whole, never cut: it costs its length, and one that doesn't fit ends the
      // object. Charged before its value is read, so no getter runs past the budget.
      if (!spend(state, 1 + advance(key, 0, state.scan.remaining)[1])) break
      const item = (value as Record<string, unknown>)[key]
      silence(item)
      if (item === undefined) continue
      if (key === '__proto__') {
        state.lossy = true
        continue
      }
      const hidden = state.secretKey?.(key) === true
      const converted = hidden ? secretValue(item, limit, state, ancestors, level + 1) : convert(item, limit, state, ancestors, level + 1)
      if (converted === NOT_JSON) {
        state.lossy = true
        continue
      }
      // Added as a literal adds it (see put()): the app's own key names, which a polluted
      // Object.prototype may hold too.
      put(out, key, converted)
      kept++
    }
    return out
  } finally {
    ancestors.delete(value)
  }
}
