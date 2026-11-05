import { createHash, randomUUID } from 'node:crypto'
import { type Budget, blank, isPlainObject, jsonLength, put, readFields, silence } from './json.js'
import { JSON_FIELD, Redactor, type Scope, TEXT_FIELD } from './redact.js'
import { advance, LIMITS, MAX_ENTRIES, NOT_JSON, ownKeys, type Shaped, type ShapeState, shapeJson, shapeString, spend } from './shape.js'
import type { DroppedReason, Json, ToolCall, TurnFields, Usage } from './types.js'

/** The ingestion's limit for IDs, and for event IDs. */
export const MAX_ID_BYTES = 512
/** Short fields kept as they are (`surface`, `apiKeyId`, names, types): longer ones are dropped as too_large. */
const MAX_SHORT_CHARS = 16_000
/**
 * A larger turnId couldn't fit in an event: dropped as too_large before it's hashed. Its UTF-8
 * bytes as the event writes it, in JSON without its quotes.
 */
const MAX_TURN_ID_BYTES = 256 * 1024
/** Work allowed to measure the non-string tool results of one event, which are never sent. */
const RESULT_MEASURE_LIMIT = 1_000_000
/** Unknown top-level fields counted for the dry run; the rest are ignored without looking. */
const MAX_IGNORED_FIELDS = 1_000

/** Fields that identify the event: checked, never shaped or redacted, and restored after `beforeSend`. */
export const ID_FIELDS = ['orgId', 'userId', 'conversationId', 'toolName', 'callId'] as const
export type Ids = Partial<Record<(typeof ID_FIELDS)[number], string>>

const TURN_FIELDS = [
  'question', 'answer', 'turnId', 'status', 'error', 'toolCalls', 'usage', 'latency', 'verification', 'client',
  'apiKeyId', 'surface',
] as const
const INPUT_FIELDS = new Set<string>([...ID_FIELDS, ...TURN_FIELDS, 'metadata'])
const TOOL_CALL_FIELDS = new Set(['name', 'input', 'resultChars', 'resultIsError', 'errorMessage', 'durationMs'])
export const SDK_FIELDS = new Set(['eventId', 'capturedAt', 'source', 'schemaVersion', 'kind', 'sdk', 'redactions', 'dropped', 'truncated'])

/**
 * Field names likely to be used instead of ours. Only these are reported by name: any other key
 * could be data, so it is counted as "(other)".
 */
const KNOWN_ALIASES = new Set([
  'tool', 'tool_name', 'toolName', 'args', 'arguments', 'params', 'parameters', 'result', 'output', 'response',
  'isError', 'is_error', 'error', 'duration', 'duration_ms', 'latency', 'latencyMs', 'id', 'type',
  'sessionId', 'session_id', 'user_id', 'user', 'messages', 'content', 'role', 'createdAt', 'timestamp',
  'inputTokens', 'outputTokens', 'cost', 'costUsd',
])

const hasOwn = (object: object, key: string) => Object.prototype.hasOwnProperty.call(object, key)
const encoder = new TextEncoder()
const utf8Length = (text: string) => encoder.encode(text).length

type Check<T> = { value: T } | { reason: DroppedReason } | undefined

/** Whether a check kept its value: never because a polluted Object.prototype has a `value`. */
export const valid = <T>(check: Check<T>): check is { value: T } => check !== undefined && hasOwn(check, 'value')

/** Shaped turn fields: strings still `Cut`s until finished with a Redactor. */
export interface ShapedTurn {
  question?: Shaped
  answer?: Shaped
  turnId?: string
  status?: 'completed' | 'error'
  error?: { message: Shaped; type?: string }
  toolCalls?: ShapedToolCall[]
  usage?: Usage
  latency?: Shaped
  verification?: Shaped
  client?: string | { name: string; version?: string }
  apiKeyId?: string
  surface?: string
}

interface ShapedToolCall extends Omit<ToolCall, 'input' | 'errorMessage'> {
  input?: Shaped
  errorMessage?: Shaped
}

/** Where shaping reports what it removed and shortened, for one event. */
export interface Notes {
  dropped: Record<string, DroppedReason>
  /** Top-level fields shortened. */
  truncated: Set<string>
  ignoredToolCallFields: Record<string, number>
}

export const newNotes = (): Notes => blank({ dropped: blank(), truncated: new Set<string>(), ignoredToolCallFields: blank() })

/** Shapes one field, recording it as truncated if anything in it was shortened. */
export function shapeField<T>(state: ShapeState, notes: Notes, name: string, run: () => T): T {
  const before = state.shortened
  state.shortened = false
  try {
    return run()
  } finally {
    if (state.shortened) notes.truncated.add(name)
    state.shortened = before || state.shortened
  }
}

/**
 * A JSON field shaped at `limit`. Not JSON: dropped as not_json; partly not JSON: kept, and
 * marked. `freeForm`: its keys come from the app's data, so secret key names apply.
 */
export function shapeAny(value: unknown, limit: number, state: ShapeState, notes: Notes, path: string, freeForm = true): Shaped | undefined {
  const lossy = state.lossy
  state.lossy = false
  state.secretKey = freeForm ? state.secretKeys : undefined
  try {
    const shaped = shapeJson(value, limit, state)
    if (shaped === NOT_JSON || state.lossy) notes.dropped[path] = 'not_json'
    return shaped === NOT_JSON ? undefined : shaped
  } finally {
    state.lossy = lossy || state.lossy
    state.secretKey = undefined
  }
}

/** Whether a string has more than `max` code points, reading at most `max + 1`. */
export function longerThan(text: string, max: number): boolean {
  // Past twice `max` UTF-16 units it has more than `max` code points: known without reading it.
  return text.length > 2 * max || (text.length > max && advance(text, 0, max + 1)[1] > max)
}

/**
 * Whether a string takes more than `max` UTF-8 bytes written as JSON, without its quotes, as the
 * event's size counts them: an escaped character, a lone surrogate among them, counts as its
 * escape. One of more than `max` code units is, unread: each is at least one byte.
 */
export function jsonBytesOver(text: string, max: number): boolean {
  return text.length > max || utf8Length(JSON.stringify(text)) - 2 > max
}

/** A field that must be a string, shaped at `limit`. */
export function shapeText(value: unknown, limit: number, state: ShapeState, notes: Notes, path: string): Shaped | undefined {
  silence(value)
  if (value == null) return undefined
  if (typeof value !== 'string') {
    notes.dropped[path] = 'wrong_type'
    return undefined
  }
  return shapeString(value, limit, state)
}

/**
 * An ID: absent, a valid string, or invalid (the event is then dropped: sent without it, it could
 * slip past a deletion block). `''` counts as absent.
 */
export function idValue(value: unknown): { value: string } | 'empty' | 'invalid' | undefined {
  if (value == null) return undefined
  if (typeof value !== 'string') return 'invalid'
  if (value === '') return 'empty'
  // Length first: it costs nothing, and an ID too long to be valid is never read.
  if (value.length > MAX_ID_BYTES || value.includes('\u0000') || utf8Length(value) > MAX_ID_BYTES) return 'invalid'
  return { value }
}

/** Reads the IDs; `invalid` lists those that would drop the event. */
export function readIds(input: Record<string, unknown>, notes: Notes, prefix = ''): { ids: Ids; invalid: string[] } {
  const ids: Ids = blank()
  const invalid: string[] = []
  for (const key of ID_FIELDS) {
    const checked = idValue(input[key])
    if (checked === 'invalid') invalid.push(`${prefix}${key}`)
    else if (checked === 'empty') notes.dropped[`${prefix}${key}`] = 'empty'
    else if (checked) ids[key] = checked.value
  }
  return { ids, invalid }
}

/**
 * `k/` and the SHA-256 of `[orgId, conversationId, turnId]` as compact JSON, a missing org as
 * null: the same for a turn whichever SDK sends it, and different for two orgs' turns.
 */
export function turnKey(orgId: string | undefined, conversationId: string, turnId: string): string {
  // Written part by part: JSON.stringify reads no toJSON on a string or null, as it would on the
  // array, where a polluted Object.prototype's would give every turn the same key.
  const json = `[${JSON.stringify(orgId ?? null)},${JSON.stringify(conversationId)},${JSON.stringify(turnId)}]`
  return `k/${createHash('sha256').update(json, 'utf8').digest('hex')}`
}

/** A turn's event ID: its `k/` key with a conversation and a turnId, or else random. */
export function turnEventId(ids: Ids, turnId: string | undefined): string {
  return ids.conversationId !== undefined && turnId !== undefined ? turnKey(ids.orgId, ids.conversationId, turnId) : randomUUID()
}

export function turnIdValue(value: unknown): Check<string> {
  if (value == null) return undefined
  if (typeof value === 'string') {
    if (value === '') return { reason: 'empty' }
    return jsonBytesOver(value, MAX_TURN_ID_BYTES) ? { reason: 'too_large' } : { value }
  }
  if (typeof value === 'number') return Number.isSafeInteger(value) ? { value: String(value) } : { reason: 'unsafe_integer' }
  return { reason: 'wrong_type' }
}

/** Reads a turn's own fields (a `turn` event's, or a call's `agent`) into shaped form. */
export function shapeTurnFields(input: Record<string, unknown>, state: ShapeState, notes: Notes, prefix = ''): ShapedTurn {
  const body: ShapedTurn = blank()
  const at = (path: string) => `${prefix}${path}`
  // A call's agent part is one field for `truncated`; a turn's fields are each their own.
  const field = <T>(name: string, run: () => T) => shapeField(state, notes, prefix ? prefix.slice(0, -1) : name, run)
  const limit = LIMITS.agent

  apply(turnIdValue(input.turnId), at('turnId'), notes, (v) => (body.turnId = v))
  apply(status(input.status), at('status'), notes, (v) => (body.status = v))
  const question = field('question', () => shapeText(input.question, limit, state, notes, at('question')))
  if (question !== undefined) body.question = question
  const answer = field('answer', () => shapeText(input.answer, limit, state, notes, at('answer')))
  if (answer !== undefined) body.answer = answer
  const error = field('error', () => errorValue(input.error, state, notes, at('error')))
  if (error !== undefined) body.error = error
  const calls = field('toolCalls', () => toolCalls(input.toolCalls, state, notes, at('toolCalls')))
  if (calls !== undefined) body.toolCalls = calls
  const usage = usageValue(input.usage, notes, at('usage'))
  if (usage) body.usage = usage
  for (const key of ['latency', 'verification'] as const) {
    if (input[key] == null) continue
    const value = field(key, () => shapeAny(input[key], limit, state, notes, at(key), true))
    if (value !== undefined) body[key] = value
  }
  apply(client(input.client, notes, at('client')), at('client'), notes, (v) => (body.client = v))
  apply(short(input.apiKeyId), at('apiKeyId'), notes, (v) => (body.apiKeyId = v))
  apply(short(input.surface), at('surface'), notes, (v) => (body.surface = v))
  return body
}

/** The scope of free-form JSON that isn't a tool input: secret key names apply, card numbers don't. */
const FREE_FORM: Scope = { ...JSON_FIELD, cards: false }

/** Shaped turn fields to JSON. `all`: redact everything; otherwise only what was cut. */
export function finishTurnFields(body: ShapedTurn, redactor: Redactor, all: boolean): TurnFields {
  const text = (value: Shaped) => redactor.finalize(value, { ...TEXT_FIELD, all }) as string
  // Ordinary objects, as `beforeSend` gets them, each field added as a literal adds it (see put()).
  const out: TurnFields = {}
  if (body.question !== undefined) put(out, 'question', text(body.question))
  if (body.answer !== undefined) put(out, 'answer', text(body.answer))
  if (body.turnId !== undefined) put(out, 'turnId', body.turnId)
  if (body.status !== undefined) put(out, 'status', body.status)
  if (body.error !== undefined) {
    const error: NonNullable<TurnFields['error']> = { message: text(body.error.message) }
    if (body.error.type !== undefined) put(error, 'type', all ? redactor.text(body.error.type) : body.error.type)
    put(out, 'error', error)
  }
  if (body.toolCalls !== undefined) {
    put(
      out,
      'toolCalls',
      body.toolCalls.map((call) => {
        const { input, errorMessage, ...rest } = call
        const finished: ToolCall = { ...rest }
        if (input !== undefined) put(finished, 'input', redactor.finalize(input, { ...JSON_FIELD, all }))
        if (errorMessage !== undefined) put(finished, 'errorMessage', text(errorMessage))
        return finished
      }),
    )
  }
  if (body.usage !== undefined) put(out, 'usage', body.usage)
  if (body.latency !== undefined) put(out, 'latency', redactor.finalize(body.latency, { ...FREE_FORM, all }))
  if (body.verification !== undefined) put(out, 'verification', redactor.finalize(body.verification, { ...FREE_FORM, all }))
  if (body.client !== undefined) put(out, 'client', body.client)
  if (body.apiKeyId !== undefined) put(out, 'apiKeyId', body.apiKeyId)
  if (body.surface !== undefined) put(out, 'surface', body.surface)
  return out
}

/**
 * Records placeholders already in shaped turn fields: in every one, redacted or not (a tool call's
 * name, `surface`, `client`), as 0.1's rule says a placeholder anywhere in the event is skipped,
 * and as `prune` finds them with a hook pending.
 */
export function reserveTurnFields(body: ShapedTurn, redactor: Redactor): void {
  redactor.reserve(body as unknown as Json)
}

export interface ShapedTurnEvent {
  ids: Ids
  fields: ShapedTurn
  metadata?: Record<string, string | number | boolean | null>
  notes: Notes
  /** Unknown top-level fields: known aliases by name, everything else as "(other)". */
  ignoredFields: Record<string, number>
}

export type TurnResult =
  | { ok: true; value: ShapedTurnEvent }
  | { ok: false; code: 'invalid_input' | 'invalid_id' | 'too_large'; paths: string[] }

/**
 * Turns `capture()` input (or `beforeSend`'s output for a turn, with `identity`) into a shaped
 * turn. Never throws on bad shapes. Only an invalid ID or oversized metadata drops it. Its options
 * are arguments, not an object's fields, so none can come from a polluted Object.prototype.
 */
export function shapeTurn(raw: unknown, state: ShapeState, metadataBudget: number, identity?: Ids & { turnId?: string }): TurnResult {
  const notes = newNotes()
  // Asked once: a Proxy's getPrototypeOf, which tells, may answer differently each time.
  const plain = isPlainObject(raw)
  if (!plain && !identity) return { ok: false, code: 'invalid_input', paths: [] }
  const input: Record<string, unknown> = plain ? readFields(raw, INPUT_FIELDS, true) : blank()
  let ids: Ids
  if (identity) {
    const { turnId, ...rest } = blank(identity)
    ids = blank(rest)
    input.turnId = turnId
  } else {
    const read = readIds(input, notes)
    if (read.invalid.length > 0) return { ok: false, code: 'invalid_id', paths: read.invalid }
    ids = read.ids
  }
  const fields = shapeTurnFields(input, state, notes)
  const metadata = metadataValue(input.metadata, notes, { remaining: metadataBudget })
  if (metadata === 'too_large') return { ok: false, code: 'too_large', paths: ['metadata'] }

  const ignoredFields: Record<string, number> = blank()
  if (plain && !identity) {
    let seen = 0
    // Its own keys, never `for...in`, which would also ask a Proxy for its prototype again, and
    // leave a promise its trap gave unhandled: JavaScript consumes that answer itself.
    for (const key of Object.keys(raw)) {
      if (++seen > MAX_IGNORED_FIELDS) break
      if (!INPUT_FIELDS.has(key) && !SDK_FIELDS.has(key)) count(ignoredFields, key)
    }
  }
  const value: ShapedTurnEvent = blank({ ids, fields, notes, ignoredFields })
  if (metadata) value.metadata = metadata
  return { ok: true, value }
}

export function apply<T>(check: Check<T>, path: string, notes: Notes, set: (value: T) => void): void {
  if (!check) return
  if (valid(check)) set(check.value)
  else notes.dropped[path] = check.reason
}

function count(counts: Record<string, number>, key: string): void {
  const name = KNOWN_ALIASES.has(key) ? key : '(other)'
  counts[name] = (counts[name] ?? 0) + 1
}

/** A short field kept as it is: at most 16,000 code points. */
export function short(value: unknown): Check<string> {
  if (value == null) return undefined
  if (typeof value !== 'string') return { reason: 'wrong_type' }
  return longerThan(value, MAX_SHORT_CHARS) ? { reason: 'too_large' } : { value }
}

function boolean(value: unknown): Check<boolean> {
  if (value == null) return undefined
  return typeof value === 'boolean' ? { value } : { reason: 'wrong_type' }
}

function number(value: unknown): Check<number> {
  if (value == null) return undefined
  return typeof value === 'number' && Number.isFinite(value) ? { value } : { reason: 'wrong_type' }
}

function status(value: unknown): Check<'completed' | 'error'> {
  if (value == null) return undefined
  return value === 'completed' || value === 'error' ? { value } : { reason: 'wrong_type' }
}

function errorValue(value: unknown, state: ShapeState, notes: Notes, path: string): ShapedTurn['error'] {
  if (value == null) return undefined
  if (typeof value === 'string') return blank({ message: shapeString(value, LIMITS.agent, state) })
  if (typeof value !== 'object') {
    notes.dropped[path] = 'wrong_type'
    return undefined
  }
  const source = readFields(value, ['message', 'type', 'name'], false) as { message?: unknown; type?: unknown; name?: unknown }
  if (typeof source.message !== 'string') {
    notes.dropped[path] = 'wrong_type'
    return undefined
  }
  const out: NonNullable<ShapedTurn['error']> = blank({ message: shapeString(source.message, LIMITS.agent, state) })
  const type = short(typeof source.type === 'string' ? source.type : source.name)
  if (valid(type)) {
    if (type.value !== '') out.type = type.value
  } else if (type?.reason === 'too_large') {
    // Too long to keep, as other short fields are; one of another type is ignored, as in 0.1.
    notes.dropped[`${path}.type`] = 'too_large'
  }
  return out
}

/** 0.1's client: a non-empty string, or `{ name, version? }`. */
export function client(value: unknown, notes?: Notes, path = 'client'): Check<string | { name: string; version?: string }> {
  if (value == null) return undefined
  if (typeof value === 'string' && value !== '') return longerThan(value, MAX_SHORT_CHARS) ? { reason: 'too_large' } : { value }
  if (!isPlainObject(value)) return { reason: 'wrong_type' }
  return named(value, notes, path)
}

/**
 * `{ name, version? }`, as clients and servers name themselves. A version too long to keep is left
 * out, and with `notes`, recorded as too_large at `path.version`; one of another type is ignored.
 */
export function named(value: unknown, notes?: Notes, path = ''): Check<{ name: string; version?: string }> {
  if (!isPlainObject(value)) return { reason: 'wrong_type' }
  const fields = readFields(value, ['name', 'version'], true)
  if (typeof fields.name !== 'string' || fields.name === '') return { reason: 'wrong_type' }
  if (longerThan(fields.name, MAX_SHORT_CHARS)) return { reason: 'too_large' }
  const out: { name: string; version?: string } = { name: fields.name }
  if (typeof fields.version === 'string') {
    if (!longerThan(fields.version, MAX_SHORT_CHARS)) put(out, 'version', fields.version)
    else if (notes) notes.dropped[`${path}.version`] = 'too_large'
  }
  return { value: out }
}

function toolCalls(value: unknown, state: ShapeState, notes: Notes, path: string): ShapedToolCall[] | undefined {
  if (value == null) return undefined
  if (!Array.isArray(value)) {
    notes.dropped[path] = 'wrong_type'
    return undefined
  }
  const out: ShapedToolCall[] = []
  const allowance: Budget = { remaining: RESULT_MEASURE_LIMIT }
  if (value.length > MAX_ENTRIES) state.shortened = true
  const end = Math.min(value.length, MAX_ENTRIES)
  for (let index = 0; index < end; index++) {
    // One per item, as for any list: past the budget, no more tool calls are taken.
    if (!spend(state, 1)) break
    const item: unknown = value[index]
    silence(item)
    const at = `${path}[${index}]`
    if (!isPlainObject(item)) {
      notes.dropped[at] = 'wrong_type'
      continue
    }
    // Counted before validation: field names an app uses instead of ours show up in the dry run.
    let seen = 0
    for (const key of ownKeys(item, state)) {
      if (++seen > MAX_IGNORED_FIELDS) break
      if (!TOOL_CALL_FIELDS.has(key)) count(notes.ignoredToolCallFields, key)
    }
    const fields = readFields(item, [...TOOL_CALL_FIELDS, 'result'], true)
    const name = short(fields.name)
    if (!valid(name) || name.value === '') {
      // A call needs its name: one too long to keep drops it as too_large.
      notes.dropped[at] = name !== undefined && !valid(name) && name.reason === 'too_large' ? 'too_large' : 'missing_name'
      continue
    }
    const call: ShapedToolCall = blank({ name: name.value })
    if (fields.input !== undefined) {
      // An explicit null input is kept: the tool was called with none.
      const input = fields.input === null ? null : shapeAny(fields.input, LIMITS.input, state, notes, `${at}.input`, true)
      if (input !== undefined) call.input = input
    }
    apply(number(fields.resultChars), `${at}.resultChars`, notes, (v) => (call.resultChars = v))
    apply(boolean(fields.resultIsError), `${at}.resultIsError`, notes, (v) => (call.resultIsError = v))
    const errorMessage = shapeText(fields.errorMessage, LIMITS.agent, state, notes, `${at}.errorMessage`)
    if (errorMessage !== undefined) call.errorMessage = errorMessage
    apply(number(fields.durationMs), `${at}.durationMs`, notes, (v) => (call.durationMs = v))
    // Results never leave: keep only their length, in code points for a string.
    if (call.resultChars === undefined && fields.result != null) {
      const length =
        typeof fields.result === 'string'
          ? resultLength(fields.result, allowance)
          : jsonLength(fields.result, allowance, (state.listed ??= new Map()))
      if (typeof length === 'number') call.resultChars = length
      else notes.dropped[`${at}.resultChars`] = length
    }
    out.push(call)
  }
  // Every item invalid means unknown, not "no tools were called".
  return out.length === 0 && value.length > 0 ? undefined : out
}

function resultLength(text: string, allowance: Budget): number | 'too_large' {
  const [end, passed] = advance(text, 0, Math.max(0, allowance.remaining) + 1)
  allowance.remaining -= passed
  return end === text.length && allowance.remaining >= 0 ? passed : 'too_large'
}

function usageValue(value: unknown, notes: Notes, path: string): Usage | undefined {
  if (value == null) return undefined
  if (!isPlainObject(value)) {
    notes.dropped[path] = 'wrong_type'
    return undefined
  }
  const fields = readFields(value, ['inputTokens', 'outputTokens', 'costUsd'], true)
  const usage: Usage = {}
  apply(number(fields.inputTokens), `${path}.inputTokens`, notes, (v) => put(usage, 'inputTokens', v))
  apply(number(fields.outputTokens), `${path}.outputTokens`, notes, (v) => put(usage, 'outputTokens', v))
  apply(number(fields.costUsd), `${path}.costUsd`, notes, (v) => put(usage, 'costUsd', v))
  return Object.keys(usage).length > 0 ? usage : undefined
}

/** 0.1's metadata: string, number, boolean and null values only, never shortened. */
export function metadataValue(
  value: unknown,
  notes: Notes,
  budget: Budget,
): Record<string, string | number | boolean | null> | undefined | 'too_large' {
  silence(value)
  if (value == null) return undefined
  if (!isPlainObject(value)) {
    notes.dropped.metadata = 'wrong_type'
    return undefined
  }
  const out: Record<string, string | number | boolean | null> = {}
  let lossy = false
  // Its own keys, as shapeTurn() lists a turn's; checked again as each is read, since a getter may
  // have removed a later one.
  for (const key of Object.keys(value)) {
    if (!hasOwn(value, key)) continue
    const item = value[key]
    silence(item)
    budget.remaining -= 1
    if (budget.remaining < 0) return 'too_large'
    if (item === undefined) continue
    const primitive =
      item === null || typeof item === 'string' || typeof item === 'boolean' || (typeof item === 'number' && Number.isFinite(item))
    if (primitive && key !== '__proto__') {
      budget.remaining -= key.length + (typeof item === 'string' ? item.length : 1)
      if (budget.remaining < 0) return 'too_large'
      put(out, key, item)
    } else {
      lossy = true
    }
  }
  if (lossy) notes.dropped.metadata = 'not_json'
  return Object.keys(out).length > 0 ? out : undefined
}

/** Metadata to JSON: secret key names apply even before the hook; with `all`, everything is redacted. */
export function finishMetadata(metadata: Record<string, string | number | boolean | null>, redactor: Redactor, all: boolean): Json {
  return redactor.finalize(metadata, { ...FREE_FORM, all })
}

export type { Json }
