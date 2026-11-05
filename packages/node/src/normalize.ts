import { createHash, randomUUID } from 'node:crypto'
import { arrayLength, type Budget, isPlainObject, jsonLength, read, readFields, toJson } from './json.js'
import type { DroppedReason, Json, ToolCall, TurnEvent, Usage } from './types.js'

/** An event without the fields the SDK adds. */
export type TurnBody = Omit<TurnEvent, 'eventId' | 'capturedAt' | 'source' | 'schemaVersion' | 'sdk' | 'redactions' | 'dropped'>

export interface Normalized {
  body: TurnBody
  dropped: Record<string, DroppedReason>
  /** Unknown top-level fields: known aliases by name, everything else as "(other)". */
  ignoredFields: Record<string, number>
  /** Tool-call fields the SDK doesn't keep, named the same way. */
  ignoredToolCallFields: Record<string, number>
}

export type NormalizeResult =
  | { ok: true; value: Normalized }
  | { ok: false; code: 'missing_required' | 'invalid_conversation_id' | 'too_large'; paths: string[] }

export const SDK_FIELDS = new Set(['eventId', 'capturedAt', 'source', 'schemaVersion', 'sdk', 'redactions', 'dropped'])

const INPUT_FIELDS = new Set([
  'conversationId', 'orgId', 'toolName', 'question', 'turnId', 'answer', 'status', 'error', 'toolCalls', 'usage',
  'latency', 'verification', 'client', 'apiKeyId', 'surface', 'metadata',
])
const TOOL_CALL_FIELDS = new Set(['name', 'input', 'resultChars', 'resultIsError', 'errorMessage', 'durationMs'])
const REQUIRED_IDS = ['conversationId', 'orgId', 'toolName'] as const

/**
 * Field names likely to be used instead of ours. Only these are reported by name: any other key
 * could be data, so it is counted as "(other)".
 */
const KNOWN_ALIASES = new Set([
  'tool', 'tool_name', 'toolName', 'args', 'arguments', 'params', 'parameters', 'result', 'output', 'response',
  'isError', 'is_error', 'error', 'duration', 'duration_ms', 'latency', 'latencyMs', 'id', 'type',
  'sessionId', 'session_id', 'userId', 'user', 'messages', 'content', 'role', 'createdAt', 'timestamp',
  'inputTokens', 'outputTokens', 'cost', 'costUsd',
])

/** Work allowed to measure the non-string tool results of one event, which are never sent. */
const RESULT_MEASURE_LIMIT = 1_000_000
/**
 * Unknown top-level fields counted for the dry run; the rest are ignored without looking at them.
 * Their keys are still listed: JavaScript lists every key of an object before the first.
 */
const MAX_IGNORED_FIELDS = 1_000
/** The ingestion rejects longer event IDs, and keeps acknowledgements small. */
export const MAX_EVENT_ID_BYTES = 512

/** Internal signal. A symbol, so no input value can be mistaken for it. */
const TOO_LARGE = Symbol('too_large')
const hasOwn = (object: object, key: string) => Object.prototype.hasOwnProperty.call(object, key)
/**
 * A field of an object the SDK built (the body, its error, client and tool calls), never one it
 * inherits: what a polluted Object.prototype gave it would be charged, or throw where the charge
 * reads it.
 */
const own = <T extends object, K extends keyof T & string>(object: T | undefined, key: K): T[K] | undefined =>
  object !== undefined && hasOwn(object, key) ? object[key] : undefined

type Check<T> = { value: T } | { reason: DroppedReason } | undefined

/** The fields `beforeSend` can't change, restored from the original capture (`turnId` even when absent). */
export interface Identity {
  conversationId: string
  orgId: string
  toolName: string
  turnId: string | undefined
}

/**
 * Turns `capture()` input (or a `beforeSend` result, with `identity`) into an event body. Never
 * throws on bad shapes. `budget` caps the work: once the content can't fit in an event, it stops as
 * `too_large`.
 */
export function normalize(raw: unknown, options: { budget?: number; identity?: Identity } = {}): NormalizeResult {
  if (!isPlainObject(raw)) {
    return { ok: false, code: 'missing_required', paths: [...REQUIRED_IDS, 'question'] }
  }
  // Every known field read once, from here on. Fields the SDK doesn't keep are never read.
  const input = readFields(raw, INPUT_FIELDS, true)
  // Its own only: capture() passes { budget }, and a polluted Object.prototype.identity would supply
  // a missing orgId, replace the IDs and the question, and give every turn the same event ID.
  const identity = hasOwn(options, 'identity') ? options.identity : undefined
  if (identity) Object.assign(input, identity)
  const missing: string[] = REQUIRED_IDS.filter((key) => typeof input[key] !== 'string' || input[key] === '')
  if (typeof input.question !== 'string') missing.push('question')
  if (missing.length > 0) return { ok: false, code: 'missing_required', paths: missing }

  const conversationId = input.conversationId as string
  if (conversationId.includes('/')) {
    return { ok: false, code: 'invalid_conversation_id', paths: ['conversationId'] }
  }

  const budget: Budget = { remaining: options.budget ?? Number.POSITIVE_INFINITY }
  const tooLarge = (): NormalizeResult => ({ ok: false, code: 'too_large', paths: [] })
  const dropped: Record<string, DroppedReason> = {}
  const body: TurnBody = {
    conversationId,
    orgId: input.orgId as string,
    toolName: input.toolName as string,
    question: input.question as string,
    status: 'completed',
  }
  budget.remaining -= conversationId.length + body.orgId.length + body.toolName.length + body.question.length
  if (budget.remaining < 0) return tooLarge()

  apply(turnId(input.turnId), 'turnId', dropped, (v) => (body.turnId = v))
  apply(string(input.answer), 'answer', dropped, (v) => (body.answer = v))
  apply(status(input.status), 'status', dropped, (v) => (body.status = v))
  apply(errorValue(input.error), 'error', dropped, (v) => (body.error = v))
  // Charged from own fields only: an `error` or `client` the body inherited made these lines throw,
  // losing every turn without its own, and a long inherited string (`type`, say) made it too_large.
  const error = own(body, 'error')
  budget.remaining -= (own(body, 'turnId')?.length ?? 0) + (own(body, 'answer')?.length ?? 0) + (error?.message.length ?? 0) + (own(error, 'type')?.length ?? 0)
  if (budget.remaining < 0) return tooLarge()

  const ignoredToolCallFields: Record<string, number> = {}
  const calls = toolCalls(input.toolCalls, dropped, ignoredToolCallFields, budget)
  if (calls === TOO_LARGE) return tooLarge()
  if (calls) body.toolCalls = calls

  const usage = usageValue(input.usage, dropped)
  if (usage) body.usage = usage

  const latency = jsonField(input.latency, 'latency', dropped, budget)
  if (latency === TOO_LARGE) return tooLarge()
  if (latency !== undefined) body.latency = latency
  const verification = jsonField(input.verification, 'verification', dropped, budget)
  if (verification === TOO_LARGE) return tooLarge()
  if (verification !== undefined) body.verification = verification

  apply(client(input.client), 'client', dropped, (v) => (body.client = v))
  apply(string(input.apiKeyId), 'apiKeyId', dropped, (v) => (body.apiKeyId = v))
  apply(string(input.surface), 'surface', dropped, (v) => (body.surface = v))
  // Charged too, so no field can make the size check serialize an oversized event.
  const clientValue = own(body, 'client')
  const clientLength = typeof clientValue === 'string' ? clientValue.length : (clientValue?.name.length ?? 0) + (own(clientValue, 'version')?.length ?? 0)
  budget.remaining -= clientLength + (own(body, 'apiKeyId')?.length ?? 0) + (own(body, 'surface')?.length ?? 0)
  if (budget.remaining < 0) return tooLarge()

  const metadata = metadataValue(input.metadata, dropped, budget)
  if (metadata === TOO_LARGE) return tooLarge()
  if (metadata) body.metadata = metadata

  const ignoredFields: Record<string, number> = {}
  let seen = 0
  try {
    // Own keys only: for...in would also walk a Proxy's prototype chain, through its getPrototypeOf trap.
    for (const key of Object.keys(raw)) {
      if (++seen > MAX_IGNORED_FIELDS) break
      if (hasOwn(raw, key) && !INPUT_FIELDS.has(key) && !SDK_FIELDS.has(key)) count(ignoredFields, key)
    }
  } catch {
    // Counted for the dry run only: keys a Proxy won't list or check are left uncounted.
  }

  return { ok: true, value: { body, dropped, ignoredFields, ignoredToolCallFields } }
}

function apply<T>(check: Check<T>, path: string, dropped: Record<string, DroppedReason>, set: (value: T) => void): void {
  if (!check) return
  // Its own: `in` would find a polluted Object.prototype's, and keep it as the field's value.
  if (hasOwn(check, 'value')) set((check as { value: T }).value)
  else dropped[path] = (check as { reason: DroppedReason }).reason
}

function count(counts: Record<string, number>, key: string): void {
  const name = KNOWN_ALIASES.has(key) ? key : '(other)'
  // Its own count: a polluted Object.prototype's would start it, inflating it or making it a string.
  counts[name] = (own(counts, name) ?? 0) + 1
}

function string(value: unknown): Check<string> {
  if (value == null) return undefined
  return typeof value === 'string' ? { value } : { reason: 'wrong_type' }
}

function boolean(value: unknown): Check<boolean> {
  if (value == null) return undefined
  return typeof value === 'boolean' ? { value } : { reason: 'wrong_type' }
}

function number(value: unknown): Check<number> {
  if (value == null) return undefined
  return typeof value === 'number' && Number.isFinite(value) ? { value } : { reason: 'wrong_type' }
}

/**
 * `${conversationId}/t/${turnId}`. When that is longer than MAX_EVENT_ID_BYTES, `h/` and its
 * SHA-256 instead: still deterministic, so the same turn always gets the same ID. Without a
 * turnId, a random ID.
 */
export function eventIdFor(conversationId: string, turnId: string | undefined): string {
  if (turnId === undefined) return randomUUID()
  const id = `${conversationId}/t/${turnId}`
  if (Buffer.byteLength(id, 'utf8') <= MAX_EVENT_ID_BYTES) return id
  return `h/${createHash('sha256').update(id, 'utf8').digest('hex')}`
}

function turnId(value: unknown): Check<string> {
  if (value == null) return undefined
  if (typeof value === 'string') return value === '' ? { reason: 'empty' } : { value }
  if (typeof value === 'number') return Number.isSafeInteger(value) ? { value: String(value) } : { reason: 'unsafe_integer' }
  return { reason: 'wrong_type' }
}

function status(value: unknown): Check<'completed' | 'error'> {
  if (value == null) return undefined
  return value === 'completed' || value === 'error' ? { value } : { reason: 'wrong_type' }
}

function errorValue(value: unknown): Check<{ message: string; type?: string }> {
  if (value == null) return undefined
  if (typeof value === 'string') return { value: { message: value } }
  if (typeof value !== 'object') return { reason: 'wrong_type' }
  // Also from its class: an Error's `name` is its prototype's. Never from Object.prototype.
  const source = readFields(value, ['message', 'type', 'name'], false) as { message?: unknown; type?: unknown; name?: unknown }
  if (typeof source.message !== 'string') return { reason: 'wrong_type' }
  const out: { message: string; type?: string } = { message: source.message }
  const type = typeof source.type === 'string' ? source.type : source.name
  if (typeof type === 'string' && type !== '') out.type = type
  return { value: out }
}

function client(value: unknown): Check<string | { name: string; version?: string }> {
  if (value == null) return undefined
  if (typeof value === 'string' && value !== '') return { value }
  if (!isPlainObject(value)) return { reason: 'wrong_type' }
  const fields = readFields(value, ['name', 'version'], true)
  if (typeof fields.name === 'string' && fields.name !== '') {
    const out: { name: string; version?: string } = { name: fields.name }
    if (typeof fields.version === 'string') out.version = fields.version
    return { value: out }
  }
  return { reason: 'wrong_type' }
}

/** Null counts as absent, except where `keepNull` says JSON null is meaningful. */
function jsonField(
  value: unknown,
  path: string,
  dropped: Record<string, DroppedReason>,
  budget: Budget,
  keepNull = false,
): Json | undefined | typeof TOO_LARGE {
  if (value === undefined || (value === null && !keepNull)) return undefined
  const result = toJson(value, budget)
  if (!result.ok) {
    if (result.tooLarge) return TOO_LARGE
    dropped[path] = 'not_json'
    return undefined
  }
  if (result.lossy) dropped[path] = 'not_json'
  return result.value
}

function toolCalls(
  value: unknown,
  dropped: Record<string, DroppedReason>,
  ignored: Record<string, number>,
  budget: Budget,
): ToolCall[] | undefined | typeof TOO_LARGE {
  if (value == null) return undefined
  let length: number | undefined
  try {
    // Read once: an Array Proxy's length is the app's code.
    length = Array.isArray(value) ? arrayLength(value) : undefined
  } catch {
    // A revoked Proxy can't even say whether it is an array.
  }
  if (length === undefined) {
    dropped.toolCalls = 'wrong_type'
    return undefined
  }
  const out: ToolCall[] = []
  const resultAllowance: Budget = { remaining: RESULT_MEASURE_LIMIT }
  for (let index = 0; index < length; index++) {
    const item = read(value as unknown[], index)
    const path = `toolCalls[${index}]`
    // Every item costs at least its slot, and a dropped one its entry in `dropped`.
    budget.remaining -= 1
    if (budget.remaining < 0) return TOO_LARGE
    if (!isPlainObject(item)) {
      dropped[path] = 'wrong_type'
      budget.remaining -= path.length
      continue
    }
    // Counted before validation: field names an app uses instead of ours show up in the dry run.
    try {
      // Own keys only, as for the top-level fields.
      for (const key of Object.keys(item)) {
        if (!hasOwn(item, key)) continue
        budget.remaining -= 1
        if (budget.remaining < 0) return TOO_LARGE
        if (!TOOL_CALL_FIELDS.has(key)) count(ignored, key)
      }
    } catch {
      // Counted for the dry run only: keys a Proxy won't list or check are left uncounted.
    }
    const fields = readFields(item, [...TOOL_CALL_FIELDS, 'result'], true)
    if (typeof fields.name !== 'string' || fields.name === '') {
      dropped[path] = 'missing_name'
      budget.remaining -= path.length
      continue
    }
    budget.remaining -= fields.name.length
    const call: ToolCall = { name: fields.name }
    const input = jsonField(fields.input, `${path}.input`, dropped, budget, true)
    if (input === TOO_LARGE || budget.remaining < 0) return TOO_LARGE
    if (input !== undefined) call.input = input
    apply(number(fields.resultChars), `${path}.resultChars`, dropped, (v) => (call.resultChars = v))
    apply(boolean(fields.resultIsError), `${path}.resultIsError`, dropped, (v) => (call.resultIsError = v))
    apply(string(fields.errorMessage), `${path}.errorMessage`, dropped, (v) => (call.errorMessage = v))
    apply(number(fields.durationMs), `${path}.durationMs`, dropped, (v) => (call.durationMs = v))
    budget.remaining -= own(call, 'errorMessage')?.length ?? 0
    // Results never leave: keep only their length. A call's own resultChars wins, but a polluted
    // Object.prototype's mustn't stand in for it.
    if (!hasOwn(call, 'resultChars') && fields.result != null) {
      const length = typeof fields.result === 'string' ? fields.result.length : jsonLength(fields.result, resultAllowance)
      if (typeof length === 'number') call.resultChars = length
      else dropped[`${path}.resultChars`] = length
    }
    out.push(call)
  }
  // Every item invalid means unknown, not "no tools called".
  return out.length === 0 && length > 0 ? undefined : out
}

function usageValue(value: unknown, dropped: Record<string, DroppedReason>): Usage | undefined {
  if (value == null) return undefined
  if (!isPlainObject(value)) {
    dropped.usage = 'wrong_type'
    return undefined
  }
  const fields = readFields(value, ['inputTokens', 'outputTokens', 'costUsd'], true)
  const usage: Usage = {}
  apply(number(fields.inputTokens), 'usage.inputTokens', dropped, (v) => (usage.inputTokens = v))
  apply(number(fields.outputTokens), 'usage.outputTokens', dropped, (v) => (usage.outputTokens = v))
  apply(number(fields.costUsd), 'usage.costUsd', dropped, (v) => (usage.costUsd = v))
  return Object.keys(usage).length > 0 ? usage : undefined
}

function metadataValue(
  value: unknown,
  dropped: Record<string, DroppedReason>,
  budget: Budget,
): Record<string, string | number | boolean | null> | undefined | typeof TOO_LARGE {
  if (value == null) return undefined
  if (!isPlainObject(value)) {
    dropped.metadata = 'wrong_type'
    return undefined
  }
  const out: Record<string, string | number | boolean | null> = {}
  let lossy = false
  try {
    // Own keys only, as for the top-level fields. Each is checked again as it is read: a getter may
    // have removed a later one.
    for (const key of Object.keys(value)) {
      if (!hasOwn(value, key)) continue
      const item = read(value, key)
      budget.remaining -= 1
      if (budget.remaining < 0) return TOO_LARGE
      if (item === undefined) continue
      const primitive =
        item === null || typeof item === 'string' || typeof item === 'boolean' || (typeof item === 'number' && Number.isFinite(item))
      if (primitive && key !== '__proto__') {
        budget.remaining -= key.length + (typeof item === 'string' ? item.length : 1)
        if (budget.remaining < 0) return TOO_LARGE
        out[key] = item
      } else {
        lossy = true
      }
    }
  } catch {
    // Keys a Proxy won't list or check (a trap that throws): what was read so far is kept.
    lossy = true
  }
  if (lossy) dropped.metadata = 'not_json'
  return Object.keys(out).length > 0 ? out : undefined
}
