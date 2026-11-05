import { MAX_EVENT_BYTES, MAX_EVENT_DEPTH, MAX_EVENT_ID_BYTES, MAX_ID_BYTES } from './limits.js'

export type RejectionReason =
  | 'missing_field'
  | 'invalid_field'
  | 'invalid_conversation_id'
  | 'too_large'
  | 'unsupported_schema_version'
  /** From the daily limits, not from validation. */
  | 'quota_exceeded'

/** What an event's `redactions` may count: the SDKs' redaction patterns. */
export type PatternName = 'email' | 'phone' | 'card' | 'secret' | 'ip'
/** Why the SDK dropped a field, as an event's `dropped` gives it. */
export type DroppedReason = 'wrong_type' | 'not_json' | 'missing_name' | 'unsafe_integer' | 'empty' | 'too_large'

/**
 * What every valid event has. The fields its kind lists are as typed when present; any other field
 * is kept as received.
 */
interface Checked {
  eventId: string
  source: 'live' | 'backfill'
  /** ISO 8601 with `Z` or an offset, as sent. */
  capturedAt: string
  [field: string]: unknown
}

/** A 0.1 turn: org, conversation, tool and question required, and no `/` in the conversation. */
export interface V1Turn extends Checked {
  schemaVersion: 1
  orgId: string
  conversationId: string
  toolName: string
  question: string
  turnId?: string
  /** Not a 0.1 field, but checked when sent, so it can't slip past a deleted person's block. */
  userId?: string
  sdk?: { name: string; version: string }
}

/** A 0.2 event: every ID is optional, except a call's tool. */
interface V2Event extends Checked {
  schemaVersion: 2
  sdk: { name: string; version: string; language: 'js' | 'python'; runtime?: string }
  orgId?: string
  userId?: string
  conversationId?: string
  callId?: string
  sessionId?: string
}

/** A 0.2 turn. Its turnId may be an integer, hashed as its decimal string. */
export interface V2Turn extends V2Event {
  kind: 'turn'
  toolName?: string
  question?: string
  turnId?: string | number
}

/** A 0.2 tool call. */
export interface V2ToolCall extends V2Event {
  kind: 'tool_call'
  toolName: string
  status: 'completed' | 'error'
  /** The reference of the turn the call was made in. */
  turnRef?: string
  /** That turn's own fields: left out for size, and never in a limited-diagnostics event. */
  agent?: Record<string, unknown>
  /** A limited-diagnostics event, which carries only the fields that can't name anyone. */
  identity?: 'failed'
}

export type WireEvent = V1Turn | V2Turn | V2ToolCall

/**
 * `event` is the value checked, as given. `json` is the event as JSON.stringify writes it, which
 * MAX_EVENT_BYTES limits: every field as received, but compact, so it can be larger than sent
 * (`9e20` becomes 21 digits).
 */
export type Validation =
  | { ok: true; event: WireEvent; json: string }
  | { ok: false; eventId: string; reason: RejectionReason }
  /** No usable eventId: rejected by its position in the request. */
  | { ok: false; eventId?: undefined; reason: RejectionReason }

type Json = Record<string, unknown>
type Check = (value: unknown) => boolean

const encoder = new TextEncoder()
export const byteLength = (text: string): number => encoder.encode(text).length

// Records, not lists, so the compiler fails if the types gain or lose a member; the SDKs' tests
// check that theirs are these.
const PATTERN_NAMES: Record<PatternName, true> = { email: true, phone: true, card: true, secret: true, ip: true }
const DROPPED_REASONS: Record<DroppedReason, true> = {
  wrong_type: true,
  not_json: true,
  missing_name: true,
  unsafe_integer: true,
  empty: true,
  too_large: true,
}
/** What a limited-diagnostics event keeps of its error. */
const ERROR_CATEGORIES = new Set(['thrown', 'tool_error', 'protocol', 'timeout'])
/**
 * Everything a limited-diagnostics event may carry. Such an event can't be deleted by person, so
 * one with anything else is rejected rather than stored.
 */
const DIAGNOSTIC_FIELDS = new Set([
  'eventId', 'kind', 'schemaVersion', 'capturedAt', 'source', 'sdk',
  'callId', 'toolName', 'status', 'durationMs', 'error', 'identity',
])

const V1_REQUIRED = ['capturedAt', 'source', 'conversationId', 'orgId', 'toolName', 'question'] as const
const V2_REQUIRED = ['kind', 'capturedAt', 'source', 'sdk'] as const
const ISO_TIME = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,9})?)?(?:Z|[+-]\d{2}:\d{2})$/
const TURN_REF = /^k\/[0-9a-f]{64}$/

const hasOwn = (object: object, key: string) => Object.prototype.hasOwnProperty.call(object, key)
/** Present, as JSON sees it: JSON has no undefined. */
const has = (object: Json, key: string) => hasOwn(object, key) && object[key] !== undefined
const isObject = (value: unknown): value is Json =>
  typeof value === 'object' && value !== null && !Array.isArray(value) && Object.getPrototypeOf(value) === Object.prototype
const isString = (value: unknown): value is string => typeof value === 'string'
const isNonEmpty = (value: unknown): value is string => typeof value === 'string' && value !== ''
const isBoolean = (value: unknown): value is boolean => typeof value === 'boolean'
// JSON.parse turns 1e400 into Infinity.
const isFiniteNumber = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value)
const anyJson = () => true
const oneOf =
  (...values: unknown[]): Check =>
  (value) =>
    values.includes(value)
const optional = (object: Json, key: string, check: Check) => !has(object, key) || check(object[key])
const allOptional = (object: Json, checks: Record<string, Check>) =>
  Object.entries(checks).every(([key, check]) => optional(object, key, check))

/** A turn's reference, or a 0.2 turn's own event ID: `k/` and 64 lowercase hex digits. */
export const isTurnRef = (value: unknown): value is string => isString(value) && TURN_REF.test(value)

/** A UTF-16 surrogate without its pair: storage doesn't give it back as sent, and no one can type it. */
const LONE_SURROGATE = /\p{Surrogate}/u
/** No NUL and no lone surrogate: what a person can type, and storage gives back as sent. */
const isTypeable = (value: string) => !value.includes('\u0000') && !LONE_SURROGATE.test(value)
/**
 * Org, person, conversation, tool, call and session IDs. A NUL or a lone surrogate can't be typed
 * in a deletion request, so an org or person with one could never be deleted.
 */
const isId = (value: unknown): value is string => isNonEmpty(value) && isTypeable(value) && byteLength(value) <= MAX_ID_BYTES
/**
 * Not limited: a turnId is only hashed, into event IDs and references, and 0.1 supports long ones
 * through its `h/` IDs. 0.2 also accepts an integer, hashed as its decimal string.
 */
const isTurnId = (value: unknown) => isNonEmpty(value) || Number.isSafeInteger(value)
const isTime = (value: unknown): value is string => isString(value) && ISO_TIME.test(value) && Number.isFinite(Date.parse(value))
const isSource = (value: unknown): value is 'live' | 'backfill' => value === 'live' || value === 'backfill'
const isStatus = oneOf('completed', 'error')
const isNamed = (value: unknown) => isObject(value) && isNonEmpty(value.name) && optional(value, 'version', isString)
const isClient = (value: unknown) => isNonEmpty(value) || isNamed(value)
const isMetadata = (value: unknown) =>
  isObject(value) && Object.values(value).every((item) => item === null || isString(item) || isBoolean(item) || isFiniteNumber(item))

const isToolCall = (value: unknown) =>
  isObject(value) &&
  isNonEmpty(value.name) &&
  optional(value, 'resultChars', isFiniteNumber) &&
  optional(value, 'resultIsError', isBoolean) &&
  optional(value, 'errorMessage', isString) &&
  optional(value, 'durationMs', isFiniteNumber)

/**
 * A turn's own fields, beyond its IDs and question: in a 0.1 event, a 0.2 `turn` and a call's
 * `agent`. `null` only passes where any JSON does. Unknown fields aren't listed: they are kept as
 * received.
 */
const TURN_FIELDS: Record<string, Check> = {
  answer: isString,
  apiKeyId: isString,
  surface: isString,
  status: isStatus,
  error: (value) => isObject(value) && isString(value.message) && optional(value, 'type', isString),
  toolCalls: (value) => Array.isArray(value) && value.every(isToolCall),
  usage: (value) =>
    isObject(value) &&
    optional(value, 'inputTokens', isFiniteNumber) &&
    optional(value, 'outputTokens', isFiniteNumber) &&
    optional(value, 'costUsd', isFiniteNumber),
  latency: anyJson,
  verification: anyJson,
  client: isClient,
  metadata: isMetadata,
}

/** Added by the SDK, in both versions. */
const BOOKKEEPING: Record<string, Check> = {
  redactions: (value) =>
    isObject(value) &&
    Object.entries(value).every(([name, count]) => hasOwn(PATTERN_NAMES, name) && Number.isSafeInteger(count) && (count as number) >= 0),
  dropped: (value) =>
    isObject(value) && Object.values(value).every((reason) => isString(reason) && hasOwn(DROPPED_REASONS, reason)),
}

const V1_OPTIONAL: Record<string, Check> = {
  ...TURN_FIELDS,
  ...BOOKKEEPING,
  turnId: isNonEmpty,
  // Not a 0.1 field, but checked and stored when sent, so it can't slip past a deleted person's block.
  userId: isId,
  sdk: (value) => isObject(value) && isString(value.name) && isString(value.version),
}

const V2_COMMON: Record<string, Check> = {
  ...BOOKKEEPING,
  sdk: (value) =>
    isObject(value) &&
    isString(value.name) &&
    isString(value.version) &&
    oneOf('js', 'python')(value.language) &&
    optional(value, 'runtime', isString),
  orgId: isId,
  userId: isId,
  conversationId: isId,
  callId: isId,
  sessionId: isId,
  sessionSource: oneOf('sent', 'issued', 'protocol'),
  client: isClient,
  protocolVersion: isString,
  metadata: isMetadata,
  truncated: (value) => Array.isArray(value) && value.every(isString),
  identity: oneOf('failed'),
}

const V2_TURN: Record<string, Check> = { ...TURN_FIELDS, toolName: isId, question: isString, turnId: isTurnId }

const V2_TOOL_CALL: Record<string, Check> = {
  toolName: isId,
  status: isStatus,
  turnRef: isTurnRef,
  server: isNamed,
  input: anyJson,
  result: isObject,
  // Limited diagnostics keep only a category; any other call has a message.
  error: (value) =>
    isObject(value) &&
    ((isString(value.message) &&
      optional(value, 'type', isString) &&
      optional(value, 'code', (code) => isFiniteNumber(code) || isString(code))) ||
      (!has(value, 'message') && ERROR_CATEGORIES.has(value.category as string))),
  durationMs: (value) => isFiniteNumber(value) && value >= 0,
  round: oneOf('input_required', 'continuation'),
  task: (value) => isObject(value) && isNonEmpty(value.id) && isString(value.status),
  intent: isString,
  extra: oneOf('missing_tools'),
  agent: (value) => isObject(value) && allOptional(value, { ...TURN_FIELDS, question: isString, turnId: isTurnId }),
}

/** Checks one event of a request, as the ingestion does before storing it. Never throws. */
export function validateEvent(value: unknown): Validation {
  if (!isObject(value)) return { ok: false, reason: 'invalid_field' }
  if (!has(value, 'eventId')) return { ok: false, reason: 'missing_field' }
  const eventId = value.eventId
  if (!isNonEmpty(eventId) || !isTypeable(eventId) || byteLength(eventId) > MAX_EVENT_ID_BYTES) {
    return { ok: false, reason: 'invalid_field' }
  }

  const reject = (reason: RejectionReason): Validation => ({ ok: false, eventId, reason })
  if (!has(value, 'schemaVersion')) return reject('missing_field')
  if (typeof value.schemaVersion !== 'number') return reject('invalid_field')
  const problem = value.schemaVersion === 1 ? checkV1(value) : value.schemaVersion === 2 ? checkV2(value) : 'unsupported_schema_version'
  if (problem !== undefined) return reject(problem)
  if (!nestsWithin(value, MAX_EVENT_DEPTH)) return reject('invalid_field')

  let json: string
  try {
    json = JSON.stringify(value)
  } catch {
    // Within the nesting limit it can't overflow the stack: a guard, so one event never fails its
    // whole request.
    return reject('invalid_field')
  }
  if (byteLength(json) > MAX_EVENT_BYTES) return reject('too_large')
  return { ok: true, event: value as WireEvent, json }
}

/**
 * Whether `value` nests at most `levels` deep, itself being the first. It stops at that depth, so
 * it never recurses deeper, whatever it is given.
 */
function nestsWithin(value: object, levels: number): boolean {
  if (levels === 0) return false
  for (const item of Array.isArray(value) ? value : Object.values(value)) {
    if (typeof item === 'object' && item !== null && !nestsWithin(item, levels - 1)) return false
  }
  return true
}

/** A 0.1 turn: org, conversation, tool and question required, and no `/` in the conversation. */
function checkV1(value: Json): RejectionReason | undefined {
  if (V1_REQUIRED.some((key) => !has(value, key))) return 'missing_field'
  const { conversationId, orgId, toolName, question, source, capturedAt } = value
  if (!isId(conversationId)) return 'invalid_field'
  if (conversationId.includes('/')) return 'invalid_conversation_id'
  if (!isId(orgId) || !isId(toolName) || !isString(question)) return 'invalid_field'
  if (!isSource(source) || !isTime(capturedAt)) return 'invalid_field'
  if (!allOptional(value, V1_OPTIONAL)) return 'invalid_field'
  return undefined
}

/** A 0.2 `tool_call` or `turn`. Every ID is optional except a call's tool. */
function checkV2(value: Json): RejectionReason | undefined {
  if (V2_REQUIRED.some((key) => !has(value, key))) return 'missing_field'
  if (isObject(value.sdk) && !has(value.sdk, 'language')) return 'missing_field'
  const { kind, source, capturedAt } = value
  if (kind !== 'turn' && kind !== 'tool_call') return 'invalid_field'
  if (kind === 'tool_call' && (!has(value, 'toolName') || !has(value, 'status'))) return 'missing_field'
  if (!isSource(source) || !isTime(capturedAt)) return 'invalid_field'
  if (!allOptional(value, V2_COMMON) || !allOptional(value, kind === 'turn' ? V2_TURN : V2_TOOL_CALL)) return 'invalid_field'
  // Only a call can fail to be identified; a diagnostics turn would take its k/ ID as a reference.
  if (value.identity === 'failed' && (kind !== 'tool_call' || !isDiagnostics(value))) return 'invalid_field'
  return undefined
}

/** Only the kept fields, and of the error only its category. */
function isDiagnostics(value: Json): boolean {
  if (Object.keys(value).some((key) => has(value, key) && !DIAGNOSTIC_FIELDS.has(key))) return false
  if (!has(value, 'error')) return true
  const error = value.error as Json
  return Object.keys(error).every((key) => key === 'category') && ERROR_CATEGORIES.has(error.category as string)
}
