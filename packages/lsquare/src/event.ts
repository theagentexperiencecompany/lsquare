import { blank, isPlainObject, put, readFields, silence } from './json.js'
import {
  apply,
  finishMetadata,
  finishTurnFields,
  idValue,
  type Ids,
  metadataValue,
  named,
  newNotes,
  type Notes,
  reserveTurnFields,
  shapeAny,
  shapeField,
  shapeText,
  type ShapedTurn,
  shapeTurn,
  shapeTurnFields,
  short,
  turnEventId,
  valid,
} from './normalize.js'
import { JSON_FIELD, Redactor, TEXT_FIELD } from './redact.js'
import { decodedSize, LIMITS, MAX_ENTRIES, newState, SCAN_BUDGET, type Shaped, type ShapeState, secretString, shapeString, spend } from './shape.js'
import type {
  DroppedReason,
  ErrorCategory,
  Json,
  LSquareEvent,
  PatternName,
  SdkInfo,
  ToolCallEvent,
  TurnEvent,
} from './types.js'

/** Parts left out, in order, until an event fits: biggest and least needed first. */
const TURN_ORDER: string[][] = [['toolCalls'], ['answer'], ['question'], ['verification'], ['latency']]
const CALL_ORDER: string[][] = [['result'], ['input'], ['agent', 'toolCalls'], ['agent']]

/** What every event needs, besides its content. */
export interface Context {
  sdk: SdkInfo
  patterns: PatternName[]
  redactKeys: readonly string[]
  maxEventBytes: number
}

/** Who a call was for: from `identify`, and from `capture()` inside the call, which wins. */
export type CallIds = Pick<Ids, 'orgId' | 'userId' | 'conversationId'>

/**
 * A call, as the wrapper saw it. Values from the app or the client are read once, checked and
 * shaped here: the wrapper passes them on as it found them.
 */
export interface CallRecord {
  callId: string
  capturedAt: string
  toolName: string
  status: 'completed' | 'error'
  durationMs: number
  ids: CallIds
  turnRef?: string
  server?: unknown
  client?: unknown
  protocolVersion?: unknown
  sessionId?: string
  sessionSource?: 'sent' | 'issued' | 'protocol'
  /** The arguments, when inputs are recorded for this tool. */
  input?: { value: unknown }
  /** MCP's result, when results are recorded for this tool. */
  result?: { value: unknown }
  error?: { message?: unknown; type?: unknown; code?: unknown; category: ErrorCategory }
  round?: 'input_required' | 'continuation'
  /** A task the call created: its ID and status come from the server, so they're shaped and redacted. */
  task?: { id: unknown; status: unknown }
  intent?: unknown
  extra?: 'missing_tools'
  /** What `capture()` sent from inside the call: from the client, its IDs, its agent part read then (`captureAgent`). */
  agent?: Record<string, unknown>
  metadata?: unknown
  /** `identify` failed: sent as limited diagnostics. */
  diagnostics: boolean
}

const RECORD_FIELDS = [
  'callId', 'capturedAt', 'toolName', 'status', 'durationMs', 'ids', 'turnRef', 'server', 'client', 'protocolVersion',
  'sessionId', 'sessionSource', 'input', 'result', 'error', 'round', 'task', 'intent', 'extra', 'agent', 'metadata', 'diagnostics',
] as const
const TURN_REF = /^k\/[0-9a-f]{64}$/

/** An event on its way: built, maybe waiting for `beforeSend`, then finished. */
export interface Draft {
  kind: 'turn' | 'tool_call'
  /** The event as `beforeSend` sees it, or as sent when there is none. */
  event: LSquareEvent
  /** Keeps numbering across `beforeSend`. */
  redactor: Redactor
  /** Fields the hook can't change, restored from here. */
  identity: Record<string, unknown>
  category: ErrorCategory | undefined
  /** The agent part's turnId, restored after the hook: the call's `turnRef` was worked out from it. */
  agentTurnId: string | undefined
  diagnostics: boolean
  notes: Notes
  ignoredFields: Record<string, number>
}

export type Built = { ok: true; draft: Draft } | { ok: false; code: 'invalid_input' | 'invalid_id' | 'too_large'; paths: string[] }

/** A turn from `capture()`. With a hook pending, only cut strings and what's under secret key names are redacted yet. */
export function buildTurn(input: unknown, capturedAt: string, context: Context, hook: boolean): Built {
  const redactor = new Redactor(context.patterns, context.redactKeys)
  const state = newState({ secretKeys: redactor.secretKeyMatcher() })
  const shaped = shapeTurn(input, state, context.maxEventBytes)
  if (!shaped.ok) return shaped
  const { ids, fields, metadata, notes, ignoredFields } = shaped.value
  // As in 0.1, a turn without a status completed.
  fields.status ??= 'completed'
  reserveTurnFields(fields, redactor)
  redactor.reserve(metadata)
  const head = {
    eventId: turnEventId(ids, fields.turnId),
    schemaVersion: 2 as const,
    kind: 'turn' as const,
    capturedAt,
    source: 'live' as const,
    sdk: context.sdk,
  }
  // No prototype: a field it lacks (a turnId) is read after the hook, and must not come from Object.prototype.
  const identity: Record<string, unknown> = blank({ ...head, ...ids })
  if (fields.turnId !== undefined) identity.turnId = fields.turnId
  // The IDs too, which are never redacted: a placeholder anywhere in the event is skipped.
  redactor.reserve(identity as unknown as Json)
  const event = turnEvent(head, ids, fields, metadata, redactor, !hook)
  const draft: Draft = { kind: 'turn', event, redactor, identity, category: undefined, agentTurnId: undefined, diagnostics: false, notes, ignoredFields }
  return pending(finish(draft, !hook, context), hook)
}

/**
 * What a draft keeps beyond its event, for the queue's limits: its redaction state, and the agent
 * part's turnId, which fitting may have left out of the event.
 */
export function retainedBytes(draft: Draft): number {
  return draft.redactor.retainedBytes() + (draft.agentTurnId?.length ?? 0) * 2
}

/** An event waiting for its hook keeps only the redaction state of what it still holds. */
function pending(built: Built, hook: boolean): Built {
  if (built.ok && hook) built.draft.redactor.prune(built.draft.event)
  return built
}

/** A call from the wrapper. */
export function buildCall(input: CallRecord, context: Context, hook: boolean): Built {
  // Every value read once, its promises silenced: some come from the app (identify, capture()).
  const record = readFields(input, RECORD_FIELDS, true) as unknown as CallRecord
  const notes = newNotes()
  const checked = checkCall(record)
  if (checked.invalid.length > 0) return { ok: false, code: 'invalid_id', paths: checked.invalid }
  // The error read once, its category included, for the event and for the draft.
  const error = isPlainObject(record.error) ? readFields(record.error, ['message', 'type', 'code', 'category'], true) : undefined
  if (record.error !== undefined && error === undefined) notes.dropped.error = 'wrong_type'
  const category = error && CATEGORIES.has(error.category as string) ? (error.category as ErrorCategory) : undefined
  const redactor = new Redactor(context.patterns, context.redactKeys)
  const state = newState({ secretKeys: redactor.secretKeyMatcher() })
  const shaped = shapeCall(record, error, category, state, notes)
  if (shaped === 'too_large') return { ok: false, code: 'too_large', paths: ['metadata'] }
  const identity = callIdentity(record, checked.ids, context, notes)
  reserveCall(shaped, identity, redactor)
  const event = callEvent(identity, shaped, redactor, !hook)
  const draft: Draft = {
    kind: 'tool_call',
    event,
    redactor,
    identity,
    category,
    agentTurnId: shaped.agent?.turnId,
    diagnostics: record.diagnostics === true,
    notes,
    ignoredFields: blank(),
  }
  return pending(finish(draft, !hook, context), hook)
}

const CATEGORIES = new Set(['thrown', 'tool_error', 'protocol', 'timeout'])

/** A call's agent part as `capture()` inside the call read it. */
interface ShapedAgent {
  body: ShapedTurn
  notes: Notes
  /** Merged with identify's when the call's event is built, and what reading it noted. */
  metadata: Metadata | 'too_large' | undefined
  metadataNotes: Notes
  /** What shaping it read of the scan budget: charged to the call's. */
  scanned: number
}

/**
 * `capture()` inside the call couldn't read its data (a getter threw): the call's event is dropped
 * when it's built, as one that couldn't be built, since what the app holds then may not be what it gave.
 */
const CAPTURE_FAILED = Symbol('capture_failed')

/** Each capture() inside a call, by the object the call's scope holds as its agent, which the wrapper passes on as it is. */
const capturedAgents = new WeakMap<object, ShapedAgent | typeof CAPTURE_FAILED>()

/** What the wrapper reads of a `capture()` inside the call, as it was given: the IDs that group the call and make its turnRef. */
const CAPTURED_IDS = ['orgId', 'userId', 'conversationId', 'turnId'] as const

/**
 * A `capture()` inside a call, as its call's scope keeps it: its IDs, as the wrapper reads them.
 * Nothing else of what the app gave is kept: the agent part is shaped by `readAgent`, after the
 * scope holds this, so a capture() from the app's code that shaping runs is a turn of its own (one
 * that reading a top-level field ran, before this was made, may have taken the scope's place).
 */
export function capturedIds(fields: Record<string, unknown>): Record<string, unknown> {
  const ids = blank()
  for (const key of CAPTURED_IDS) if (fields[key] !== undefined) ids[key] = fields[key]
  return ids
}

/**
 * Reads a call's agent part when `capture()` is called inside the call, as any capture() reads its
 * data before it returns (0.1's rule): what the app does with its objects afterwards changes
 * nothing. A read that throws is final too: never read again. `captured` is what the scope holds
 * (`capturedIds`), `fields` capture()'s, read once.
 */
export function readAgent(captured: object, fields: Record<string, unknown>, context: Context): void {
  let read: ShapedAgent | typeof CAPTURE_FAILED
  try {
    read = shapeAgent(fields, context)
  } catch {
    read = CAPTURE_FAILED
  }
  capturedAgents.set(captured, read)
}

/** A call's agent part, shaped as for its event, alone: what it reads is charged to the call's scan budget in the agent's place (`charge`). */
function shapeAgent(fields: Record<string, unknown>, context: Context): ShapedAgent {
  const redactor = new Redactor(context.patterns, context.redactKeys)
  const state = newState({ secretKeys: redactor.secretKeyMatcher() })
  const notes = newNotes()
  const body = shapeTurnFields(readFields(fields, AGENT_FIELDS, true), state, notes, 'agent.')
  // As for a turn of its own: one without a status completed.
  body.status ??= 'completed'
  const metadataNotes = newNotes()
  const metadata = metadataValue(fields.metadata, metadataNotes, { remaining: METADATA_BUDGET })
  return { body, notes, metadata, metadataNotes, scanned: SCAN_BUDGET - state.scan.remaining }
}

/**
 * The scan budget an agent part read when it was captured, charged in its place: with more left
 * than it read, what's left for the result is what shaping it now would leave, and so was the agent
 * part. With less, the budget ran out there: it keeps what it read, the result none.
 */
function charge(state: ShapeState, early: ShapedAgent): void {
  state.scan.remaining = state.scan.remaining > early.scanned ? state.scan.remaining - early.scanned : 0
}

/** Notes taken earlier, in their place among the event's. */
function addNotes(into: Notes, from: Notes): void {
  Object.assign(into.dropped, from.dropped)
  for (const name of from.truncated) into.truncated.add(name)
  for (const [key, count] of Object.entries(from.ignoredToolCallFields)) into.ignoredToolCallFields[key] = (into.ignoredToolCallFields[key] ?? 0) + count
}

/** The call's IDs and outline, checked as the ingestion checks them. */
function checkCall(record: CallRecord): { ids: CallIds; invalid: string[] } {
  const invalid: string[] = []
  const ids: CallIds = blank()
  for (const key of ['callId', 'toolName'] as const) if (typeof idValue(record[key]) !== 'object') invalid.push(key)
  const given = isPlainObject(record.ids) ? readFields(record.ids, ['orgId', 'userId', 'conversationId'], true) : blank()
  for (const key of ['orgId', 'userId', 'conversationId'] as const) {
    const checked = idValue(given[key])
    if (checked === 'invalid') invalid.push(`ids.${key}`)
    else if (typeof checked === 'object') ids[key] = checked.value
  }
  if (record.sessionId !== undefined && typeof idValue(record.sessionId) !== 'object') invalid.push('sessionId')
  if (record.turnRef !== undefined && !(typeof record.turnRef === 'string' && TURN_REF.test(record.turnRef))) invalid.push('turnRef')
  return { ids, invalid }
}

/**
 * `beforeSend`'s output, checked like the app's input: identity restored, shaped again (strings
 * already shortened keep their markers), everything redacted, fitted, and diagnostics reduced.
 * Values removed from it are recorded under `beforeSend.`.
 */
export function afterHook(out: Record<string, unknown>, draft: Draft, context: Context): Built {
  const redactor = draft.redactor
  const state = newState({ afterHook: true, cuts: redactor.cuts, secretKeys: redactor.secretKeyMatcher() })
  const hookNotes = newNotes()
  let event: LSquareEvent
  if (draft.kind === 'turn') {
    const { eventId, schemaVersion, kind, capturedAt, source, sdk, turnId, ...ids } = draft.identity
    const shaped = shapeTurn(out, state, context.maxEventBytes, { ...(ids as Ids), ...(turnId === undefined ? {} : { turnId: turnId as string }) })
    if (!shaped.ok) return shaped
    shaped.value.fields.status ??= 'completed'
    reserveTurnFields(shaped.value.fields, redactor)
    redactor.reserve(shaped.value.metadata)
    redactor.reserve(draft.identity as unknown as Json)
    mergeNotes(hookNotes, shaped.value.notes)
    const head = { eventId, schemaVersion, kind, capturedAt, source, sdk } as unknown as Pick<TurnEvent, 'eventId' | 'schemaVersion' | 'kind' | 'capturedAt' | 'source' | 'sdk'>
    event = turnEvent(head, ids as Ids, shaped.value.fields, shaped.value.metadata, redactor, true)
  } else {
    const shaped = reshapeCall(out, draft, state, hookNotes)
    if (shaped === 'too_large') return { ok: false, code: 'too_large', paths: ['metadata'] }
    // No prototype, as the draft's (see callIdentity()).
    const identity = blank({ ...draft.identity })
    // The hook may remove or change what the calling app and the server called themselves.
    for (const key of ['client', 'server', 'protocolVersion']) {
      delete identity[key]
      if (shaped.fixed[key] !== undefined) identity[key] = shaped.fixed[key]
    }
    reserveCall(shaped, identity, redactor)
    event = callEvent(identity, shaped, redactor, true)
  }
  const notes = draft.notes
  for (const [path, reason] of Object.entries(hookNotes.dropped)) notes.dropped[`beforeSend.${path}`] = reason
  for (const name of hookNotes.truncated) notes.truncated.add(name)
  return finish({ ...draft, event, notes }, true, context)
}

/** Adds the SDK's bookkeeping, fits the event, and once it's final, reduces diagnostics. */
function finish(draft: Draft, final: boolean, context: Context): Built {
  // Diagnostics keep only a few small fields, whatever the full event's size.
  if (final && draft.diagnostics) {
    draft.event = diagnostics(draft)
    return { ok: true, draft }
  }
  const event = draft.event as unknown as Record<string, unknown>
  const { notes, redactor } = draft
  // Each added as a literal adds it (see put()), as every field of the event is.
  if (final && Object.keys(redactor.counts).length > 0) put(event, 'redactions', { ...redactor.counts })
  if (Object.keys(notes.dropped).length > 0) put(event, 'dropped', { ...notes.dropped })
  if (notes.truncated.size > 0) put(event, 'truncated', [...notes.truncated])
  const fits = fit(event, context.maxEventBytes, draft.kind === 'turn' ? TURN_ORDER : CALL_ORDER, notes.truncated)
  // A diagnostics event waiting for its hook is reduced after it, so its size doesn't count yet.
  if (!fits && !draft.diagnostics) return { ok: false, code: 'too_large', paths: [] }
  return { ok: true, draft }
}

/** Leaves parts out, in order, until the event's compact JSON fits. False if it never does. */
function fit(event: Record<string, unknown>, maxBytes: number, order: string[][], truncated: Set<string>): boolean {
  let bytes = byteSize(event)
  for (const path of order) {
    if (bytes <= maxBytes) return true
    const parent = path.length === 1 ? event : (event[path[0] as string] as Record<string, unknown> | undefined)
    const key = path[path.length - 1] as string
    if (!parent || !Object.prototype.hasOwnProperty.call(parent, key)) continue
    delete parent[key]
    truncated.add(path.join('.'))
    put(event, 'truncated', [...truncated])
    bytes = byteSize(event)
  }
  return bytes <= maxBytes
}

/** What a limited-diagnostics event keeps: what every event needs, and the call's outline. */
function diagnostics(draft: Draft): ToolCallEvent {
  const event = draft.event as ToolCallEvent
  const out: ToolCallEvent = {
    eventId: event.eventId,
    kind: 'tool_call',
    schemaVersion: 2,
    capturedAt: event.capturedAt,
    source: 'live',
    sdk: event.sdk,
    callId: event.callId,
    toolName: event.toolName,
    status: event.status,
    durationMs: event.durationMs,
    identity: 'failed',
  }
  if (draft.category !== undefined) put(out, 'error', { category: draft.category })
  return out
}

export function byteSize(value: unknown): number {
  return utf8.encode(JSON.stringify(value)).length
}
const utf8 = new TextEncoder()

function mergeNotes(into: Notes, from: Notes): void {
  Object.assign(into.dropped, from.dropped)
  for (const name of from.truncated) into.truncated.add(name)
}

function turnEvent(
  head: Pick<TurnEvent, 'eventId' | 'schemaVersion' | 'kind' | 'capturedAt' | 'source' | 'sdk'>,
  ids: Ids,
  fields: ShapedTurn,
  metadata: Record<string, string | number | boolean | null> | undefined,
  redactor: Redactor,
  all: boolean,
): TurnEvent {
  const event: TurnEvent = { ...head, ...ids, ...finishTurnFields(fields, redactor, all) }
  if (metadata) put(event, 'metadata', finishMetadata(metadata, redactor, all) as Metadata)
  return event
}

// Calls.

const RESULT_KEYS = ['content', 'structuredContent', 'isError']
const BLOCK_KEYS = ['type', 'text', 'data', 'mimeType', 'uri', 'name', 'resource', 'bytes']

interface ShapedBlock {
  type: string
  text?: Shaped
  uri?: Shaped
  name?: Shaped
  mimeType?: Shaped
  bytes?: number
}

interface ShapedCall {
  /** Fixed fields that `beforeSend` may change: what the calling app and the server called themselves. */
  fixed: Record<string, Json>
  task?: { id: string; status: string }
  input?: Shaped
  result?: { content?: ShapedBlock[]; structuredContent?: Shaped; isError?: boolean }
  error?: { message?: Shaped; type?: string; code?: number | string; category?: ErrorCategory }
  intent?: Shaped
  agent?: ShapedTurn
  metadata?: Record<string, string | number | boolean | null>
}

type Metadata = Record<string, string | number | boolean | null>

function callIdentity(record: CallRecord, ids: CallIds, context: Context, notes: Notes): Record<string, unknown> {
  // No prototype, as a turn's: the fields added to it are never a polluted Object.prototype's to
  // refuse (one that isn't writable would make the assignment throw), nor one it lacks read from it.
  const identity: Record<string, unknown> = blank({
    eventId: record.callId,
    schemaVersion: 2,
    kind: 'tool_call',
    capturedAt: record.capturedAt,
    source: 'live',
    sdk: context.sdk,
    callId: record.callId,
    toolName: record.toolName,
    ...ids,
  })
  if (record.turnRef !== undefined) identity.turnRef = record.turnRef
  namedFields(record as unknown as Record<string, unknown>, identity, notes)
  if (record.sessionId !== undefined) identity.sessionId = record.sessionId
  if (record.sessionSource !== undefined) identity.sessionSource = record.sessionSource
  identity.status = record.status
  identity.durationMs = record.durationMs
  if (record.round !== undefined) identity.round = record.round
  if (record.extra !== undefined) identity.extra = record.extra
  return identity
}

/**
 * What the server and the calling app called themselves, and the protocol version, as the protocol
 * (or `beforeSend`) gave them. One too long to keep is left out and recorded as too_large, as other
 * short fields are; one of another type is left out without a word, as the protocol may not give one.
 */
function namedFields(source: Record<string, unknown>, into: Record<string, unknown>, notes: Notes): void {
  for (const key of ['server', 'client'] as const) {
    const checked = named(source[key], notes, key)
    if (valid(checked)) into[key] = checked.value
    else if (checked?.reason === 'too_large') notes.dropped[key] = 'too_large'
  }
  const protocol = short(source.protocolVersion)
  if (valid(protocol)) into.protocolVersion = protocol.value
  else if (protocol?.reason === 'too_large') notes.dropped.protocolVersion = 'too_large'
}

/** A task's ID and status: short strings, both needed. */
function taskValue(value: unknown, notes: Notes, path: string): ShapedCall['task'] {
  if (value == null) return undefined
  const fields = isPlainObject(value) ? readFields(value, ['id', 'status'], true) : blank()
  const id = short(fields.id)
  const status = short(fields.status)
  if (valid(id) && id.value !== '' && valid(status)) return { id: id.value, status: status.value }
  // Both strings, the ID not empty, but one too long to keep: too_large, as other short fields are.
  const strings = [id, status].every((check) => check !== undefined && (valid(check) || check.reason === 'too_large'))
  const empty = valid(id) && id.value === ''
  notes.dropped[path] = strings && !empty ? 'too_large' : 'wrong_type'
  return undefined
}

/** An error's message, type and code, as the app or the client gave them. */
function errorFields(fields: Record<string, unknown>, state: ShapeState, notes: Notes, field: <T>(name: string, run: () => T) => T): NonNullable<ShapedCall['error']> {
  const error: NonNullable<ShapedCall['error']> = blank()
  const text = field('error', () => shapeText(fields.message, LIMITS.result, state, notes, 'error.message'))
  if (text !== undefined) error.message = text
  apply(short(fields.type), 'error.type', notes, (v) => v !== '' && (error.type = v))
  if (typeof fields.code === 'number' && Number.isFinite(fields.code)) error.code = fields.code
  else apply(short(fields.code), 'error.code', notes, (v) => (error.code = v))
  return error
}

/**
 * Shaped in a fixed order, which the scan budget makes part of the result: the intent and error
 * first, as the smallest and most telling, then the input, the agent's turn and the result.
 * Oversized metadata drops the event, as for a turn, unless it's diagnostics, which leave it out.
 */
function shapeCall(
  record: CallRecord,
  error: Record<string, unknown> | undefined,
  category: ErrorCategory | undefined,
  state: ShapeState,
  notes: Notes,
): ShapedCall | 'too_large' {
  const shaped: ShapedCall = blank({ fixed: blank() })
  const field = <T>(name: string, run: () => T) => shapeField(state, notes, name, run)
  const task = taskValue(record.task, notes, 'task')
  if (task) shaped.task = task
  if (record.intent !== undefined) {
    const intent = field('intent', () => shapeText(record.intent, LIMITS.intent, state, notes, 'intent'))
    if (intent !== undefined) shaped.intent = intent
  }
  if (error) {
    const fields = errorFields(error, state, notes, field)
    if (category !== undefined) fields.category = category
    // Neither a message nor a category: nothing the ingestion would take as an error.
    if (fields.message !== undefined || fields.category !== undefined) shaped.error = fields
    else notes.dropped.error = 'wrong_type'
  }
  if (record.input) {
    const value = isPlainObject(record.input) ? readFields(record.input, ['value'], true).value : undefined
    const input = field('input', () => shapeAny(value ?? {}, LIMITS.input, state, notes, 'input', true))
    if (input !== undefined) shaped.input = input
  }
  // Read when capture() was called. One that couldn't be read then isn't read now: what the app
  // holds now may not be what it gave.
  const early = typeof record.agent === 'object' && record.agent !== null ? capturedAgents.get(record.agent) : undefined
  if (early === CAPTURE_FAILED) throw new Error("capture() inside the call couldn't read its data")
  if (early) {
    shaped.agent = early.body
    addNotes(notes, early.notes)
    charge(state, early)
  } else if (record.agent != null) {
    if (isPlainObject(record.agent)) {
      shaped.agent = shapeTurnFields(readFields(record.agent, AGENT_FIELDS, true), state, notes, 'agent.')
      // As for a turn of its own: one without a status completed.
      shaped.agent.status ??= 'completed'
    } else {
      notes.dropped.agent = 'wrong_type'
    }
  }
  if (record.result) {
    const value = isPlainObject(record.result) ? readFields(record.result, ['value'], true).value : undefined
    const result = field('result', () => shapeResult(value, state, notes, false))
    if (result !== undefined) shaped.result = result
  }
  // identify's metadata, merged with that of the capture() inside the call, which wins per key.
  // Each is read within the budget, and the merged result must fit it too: a value one replaces
  // doesn't count.
  const identified = metadataValue(record.metadata, notes, { remaining: METADATA_BUDGET })
  if (early) addNotes(notes, early.metadataNotes)
  const captured = early
    ? early.metadata
    : isPlainObject(record.agent)
      ? metadataValue(readFields(record.agent, ['metadata'], true).metadata, notes, { remaining: METADATA_BUDGET })
      : undefined
  const merged = identified === 'too_large' || captured === 'too_large' ? 'too_large' : identified || captured ? { ...identified, ...captured } : undefined
  if (merged === 'too_large' || (merged && metadataSize(merged) > METADATA_BUDGET)) {
    if (!record.diagnostics) return 'too_large'
  } else if (merged) {
    shaped.metadata = merged
  }
  return shaped
}

/** Metadata's size as its budget counts it: one per entry, its key, and a string's length. */
function metadataSize(metadata: Metadata): number {
  let size = 0
  for (const [key, value] of Object.entries(metadata)) size += 1 + key.length + (typeof value === 'string' ? value.length : 1)
  return size
}

/**
 * `beforeSend`'s output for a call: shaped again by the same rules, the result's blocks included,
 * so a hook can't add a file's data back. The error keeps its category, which says what happened
 * when the message is left out; the agent part keeps its turnId, which the call's turnRef is from.
 */
function reshapeCall(out: Record<string, unknown>, draft: Draft, state: ShapeState, notes: Notes): ShapedCall | 'too_large' {
  const input = readFields(out, ['input', 'result', 'error', 'intent', 'agent', 'metadata', 'task', 'client', 'server', 'protocolVersion'], true)
  const shaped: ShapedCall = blank({ fixed: blank() })
  const field = <T>(name: string, run: () => T) => shapeField(state, notes, name, run)
  namedFields(input, shaped.fixed, notes)
  const task = taskValue(input.task, notes, 'task')
  if (task) shaped.task = task
  if (input.intent != null) {
    const intent = field('intent', () => shapeText(input.intent, LIMITS.intent, state, notes, 'intent'))
    if (intent !== undefined) shaped.intent = intent
  }
  if (input.error != null) {
    if (isPlainObject(input.error)) {
      const error = errorFields(readFields(input.error, ['message', 'type', 'code'], true), state, notes, field)
      if (draft.category !== undefined) error.category = draft.category
      if (error.message !== undefined || error.category !== undefined) shaped.error = error
    } else {
      notes.dropped.error = 'wrong_type'
      // Replaced by something that isn't an error: the category stays, as below.
      if (draft.category !== undefined) shaped.error = blank({ category: draft.category })
    }
  } else if (draft.category !== undefined) {
    // Removed by the hook: the category, which isn't content, stays.
    shaped.error = blank({ category: draft.category })
  }
  if (input.input !== undefined) {
    const value = field('input', () => shapeAny(input.input, LIMITS.input, state, notes, 'input', true))
    if (value !== undefined) shaped.input = value
  }
  if (input.agent != null) {
    if (isPlainObject(input.agent)) {
      const agent = shapeTurnFields(readFields(input.agent, AGENT_FIELDS, true), state, notes, 'agent.')
      delete agent.turnId
      if (draft.agentTurnId !== undefined) agent.turnId = draft.agentTurnId
      shaped.agent = agent
    } else {
      notes.dropped.agent = 'wrong_type'
    }
  }
  if (input.result != null) {
    const result = field('result', () => shapeResult(input.result, state, notes, true))
    if (result !== undefined) shaped.result = result
  }
  const metadata = metadataValue(input.metadata, notes, { remaining: METADATA_BUDGET })
  if (metadata === 'too_large') {
    if (!draft.diagnostics) return 'too_large'
  } else if (metadata) {
    shaped.metadata = metadata
  }
  return shaped
}

/** Metadata's own budget, as for a turn: the whole event's size. */
const METADATA_BUDGET = 256 * 1024

const AGENT_FIELDS = [
  'question', 'answer', 'turnId', 'status', 'error', 'toolCalls', 'usage', 'latency', 'verification', 'client',
  'apiKeyId', 'surface',
]

/**
 * MCP's result without anything binary: files become their size, text is shortened. `sized`:
 * `beforeSend`'s output, whose files may already be only their size.
 */
function shapeResult(value: unknown, state: ShapeState, notes: Notes, sized: boolean): ShapedCall['result'] | undefined {
  silence(value)
  if (!isPlainObject(value)) {
    notes.dropped.result = 'wrong_type'
    return undefined
  }
  const fields = readFields(value, RESULT_KEYS, true)
  const out: NonNullable<ShapedCall['result']> = blank()
  if (Array.isArray(fields.content)) {
    const content = fields.content as unknown[]
    if (content.length > MAX_ENTRIES) state.shortened = true
    const blocks: ShapedBlock[] = []
    for (let index = 0; index < Math.min(content.length, MAX_ENTRIES); index++) {
      // One per block, as for any list: past the budget, no more blocks are taken.
      if (!spend(state, 1)) break
      const block = shapeBlock(content[index], state, sized)
      if (block) blocks.push(block)
      else notes.dropped[`result.content[${index}]`] = 'wrong_type'
    }
    out.content = blocks
  } else if (fields.content !== undefined) {
    notes.dropped['result.content'] = 'wrong_type'
  }
  if (fields.structuredContent !== undefined) {
    const structured = shapeAny(fields.structuredContent, LIMITS.result, state, notes, 'result.structuredContent', true)
    if (structured !== undefined) out.structuredContent = structured
  }
  if (typeof fields.isError === 'boolean') out.isError = fields.isError
  return out
}

function shapeBlock(value: unknown, state: ShapeState, sized: boolean): ShapedBlock | undefined {
  silence(value)
  if (!isPlainObject(value)) return undefined
  const block = readFields(value, BLOCK_KEYS, true)
  const type = typeof block.type === 'string' && block.type !== '' && block.type.length <= 64 ? block.type : undefined
  if (type === undefined) return undefined
  // A block's fields are MCP's, but a secret key name or one of `redactKeys` still replaces one
  // whole, numbered by its raw value.
  const hidden = (key: string) => state.secretKeys?.(key) === true
  const text = (field: unknown, key: string) => {
    if (typeof field !== 'string') return undefined
    return hidden(key) ? secretString(field, LIMITS.result, state) : shapeString(field, LIMITS.result, state)
  }
  const mime = (field: unknown) => {
    if (typeof field !== 'string' || field.length > 256) return undefined
    return hidden('mimeType') ? secretString(field, 256, state) : field
  }
  const out: ShapedBlock = blank({ type })
  const set = <K extends keyof ShapedBlock>(key: K, item: ShapedBlock[K] | undefined) => {
    if (item !== undefined) out[key] = item
  }
  // A size this SDK worked out before the hook: kept, as nothing else of the file is.
  const sizeOnly = () => {
    if (sized && typeof block.bytes === 'number' && Number.isSafeInteger(block.bytes) && block.bytes >= 0) out.bytes = block.bytes
  }
  switch (type) {
    case 'text':
      set('text', text(block.text, 'text') ?? '')
      break
    case 'image':
    case 'audio':
      set('mimeType', mime(block.mimeType))
      if (typeof block.data === 'string') {
        out.bytes = decodedSize(block.data.length, block.data)
        state.shortened = true
      } else {
        sizeOnly()
      }
      break
    case 'resource_link':
      set('uri', text(block.uri, 'uri'))
      set('name', text(block.name, 'name'))
      set('mimeType', mime(block.mimeType))
      break
    case 'resource': {
      // After the hook, the block is in this SDK's flat form; before it, in MCP's.
      const resource = isPlainObject(block.resource) ? readFields(block.resource, ['uri', 'mimeType', 'text', 'blob'], true) : sized ? block : blank()
      set('uri', text(resource.uri, 'uri'))
      set('mimeType', mime(resource.mimeType))
      if (typeof resource.text === 'string') {
        out.text = text(resource.text, 'text') as Shaped
      } else if (typeof resource.blob === 'string') {
        out.bytes = decodedSize(resource.blob.length, resource.blob)
        state.shortened = true
      } else {
        sizeOnly()
      }
      break
    }
    default:
      // Unknown kinds keep only their type: their fields could hold anything.
      break
  }
  return out
}

/**
 * Records placeholders already in the call's event: in every field, redacted or not (its tool name,
 * IDs, client and server, a block's type), as for a turn.
 */
function reserveCall(shaped: ShapedCall, identity: Record<string, unknown>, redactor: Redactor): void {
  redactor.reserve(shaped as unknown as Json)
  redactor.reserve(identity as unknown as Json)
}

function callEvent(identity: Record<string, unknown>, shaped: ShapedCall, redactor: Redactor, all: boolean): ToolCallEvent {
  const text = (value: Shaped) => redactor.finalize(value, { ...TEXT_FIELD, all }) as string
  // Ordinary objects, as `beforeSend` gets them, each field added as a literal adds it (see put()).
  const event = { ...identity } as unknown as ToolCallEvent
  if (shaped.input !== undefined) put(event, 'input', redactor.finalize(shaped.input, { ...JSON_FIELD, all }))
  if (shaped.result !== undefined) {
    const result: Record<string, Json> = {}
    if (shaped.result.content) {
      put(
        result,
        'content',
        shaped.result.content.map((block) => {
          const out: Record<string, Json> = { type: text(block.type) }
          if (block.uri !== undefined) put(out, 'uri', text(block.uri))
          if (block.name !== undefined) put(out, 'name', text(block.name))
          if (block.mimeType !== undefined) put(out, 'mimeType', text(block.mimeType))
          if (block.text !== undefined) put(out, 'text', text(block.text))
          if (block.bytes !== undefined) put(out, 'bytes', block.bytes)
          return out
        }),
      )
    }
    if (shaped.result.structuredContent !== undefined) {
      put(result, 'structuredContent', redactor.finalize(shaped.result.structuredContent, { ...JSON_FIELD, all }))
    }
    if (shaped.result.isError !== undefined) put(result, 'isError', shaped.result.isError)
    put(event, 'result', result)
  }
  if (shaped.error !== undefined) {
    const error: NonNullable<ToolCallEvent['error']> = {}
    if (shaped.error.message !== undefined) put(error, 'message', text(shaped.error.message))
    if (shaped.error.type !== undefined) put(error, 'type', all ? redactor.text(shaped.error.type) : shaped.error.type)
    if (shaped.error.code !== undefined) put(error, 'code', typeof shaped.error.code === 'string' && all ? redactor.text(shaped.error.code) : shaped.error.code)
    if (shaped.error.category !== undefined) put(error, 'category', shaped.error.category)
    put(event, 'error', error)
  }
  if (shaped.intent !== undefined) put(event, 'intent', text(shaped.intent))
  if (shaped.task !== undefined) put(event, 'task', { id: text(shaped.task.id), status: text(shaped.task.status) })
  if (shaped.agent !== undefined) put(event, 'agent', finishTurnFields(shaped.agent, redactor, all))
  if (shaped.metadata) put(event, 'metadata', finishMetadata(shaped.metadata, redactor, all) as Metadata)
  return event
}

export type { DroppedReason }
