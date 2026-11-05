export type Json = string | number | boolean | null | Json[] | { [key: string]: Json }

/** Built-in redaction patterns. */
export type PatternName = 'email' | 'phone' | 'card' | 'secret' | 'ip'

/** Why a value was removed from an event. Recorded in the event's `dropped`. */
export type DroppedReason = 'wrong_type' | 'not_json' | 'missing_name' | 'unsafe_integer' | 'empty' | 'too_large'

/** Why an event was dropped, failed or is still pending, or why sending is held back. Passed to `onError`. */
export type ErrorCode =
  /** `capture()` was given something other than a plain object. */
  | 'invalid_input'
  /** An ID (`orgId`, `userId`, `conversationId`, `toolName`, `callId`) that isn't a non-empty string of at most 512 bytes without NUL. */
  | 'invalid_id'
  /** 0.1's: `conversationId` with `/`. 0.2 accepts those, so it no longer reports it. */
  | 'invalid_conversation_id'
  /** 0.1's: a required field missing. 0.2 has none, so it no longer reports it. */
  | 'missing_required'
  | 'too_large'
  | 'queue_full'
  | 'hook_failed'
  | 'hook_timeout'
  | 'redaction_failed'
  | 'send_failed'
  | 'rejected'
  /** The project's daily limit was reached: events are dropped until `resetsAt`. */
  | 'quota_exceeded'
  /** Sending resumed after a daily limit: `count` events were dropped meanwhile. */
  | 'quota_dropped'
  | 'pending_after_flush'
  /** `identify` threw, took too long, was skipped at the hook limit, or returned invalid IDs: the call was sent as limited diagnostics. */
  | 'identify_failed'
  | 'shutdown'
  | 'internal_error'

export interface LSquareError {
  code: ErrorCode
  /** Never contains event content. */
  message: string
  eventId?: string
  paths?: string[]
  /** HTTP status, when the error comes from a response. */
  status?: number
  /** `quota_exceeded`: when the daily limit resets. */
  resetsAt?: string
  /** `quota_dropped`: how many events were dropped. */
  count?: number
  /** Reports dropped by the rate limit since the last one delivered. */
  suppressed?: number
}

/**
 * What `capture()` accepts: 0.1's fields, all optional, plus `userId` and `callId`. Fields whose
 * shape varies between apps take any value: anything the SDK can't use is removed at runtime and
 * recorded in `dropped`, never a compile error.
 */
export interface CaptureInput {
  conversationId?: string | null | undefined
  orgId?: string | null | undefined
  /** An internal ID, not an email address: events can be deleted by it. */
  userId?: string | null | undefined
  toolName?: string | null | undefined
  question?: string | null | undefined
  turnId?: string | number | null | undefined
  answer?: string | null | undefined
  status?: 'completed' | 'error' | undefined
  error?: unknown
  toolCalls?: unknown
  usage?:
    | {
        inputTokens?: number | null | undefined
        outputTokens?: number | null | undefined
        costUsd?: number | null | undefined
      }
    | null
    | undefined
  latency?: unknown
  verification?: unknown
  client?: unknown
  apiKeyId?: string | null | undefined
  surface?: string | undefined
  metadata?: Record<string, unknown> | null | undefined
  /** Links a turn sent after its call, e.g. from a background job, to that call. */
  callId?: string | null | undefined
}

export interface ToolCall {
  /** Internal tool, e.g. 'search_docs'. */
  name: string
  input?: Json
  resultChars?: number
  resultIsError?: boolean
  errorMessage?: string
  durationMs?: number
}

export interface Usage {
  inputTokens?: number
  outputTokens?: number
  costUsd?: number
}

export interface SdkInfo {
  name: string
  version: string
  language: 'js' | 'python'
  /** The runtime and its major version, e.g. `node/22`, `bun/1.2`, `workers`. */
  runtime?: string
}

/** A turn's own fields: those of a `turn` event, and of a call's `agent` part. */
export interface TurnFields {
  question?: string
  answer?: string
  turnId?: string
  status?: 'completed' | 'error'
  error?: { message: string; type?: string }
  /** Absent means not recorded or all invalid; `[]` means no tools were called. */
  toolCalls?: ToolCall[]
  /** This turn only. */
  usage?: Usage
  latency?: Json
  verification?: Json
  client?: string | { name: string; version?: string }
  apiKeyId?: string
  surface?: string
}

interface EventBase {
  eventId: string
  schemaVersion: 2
  capturedAt: string
  source: 'live'
  sdk: SdkInfo
  orgId?: string
  userId?: string
  conversationId?: string
  metadata?: Record<string, string | number | boolean | null>
  /** Replacements made by built-in redaction, per pattern. */
  redactions?: Partial<Record<PatternName, number>>
  /** Values the SDK removed: path → reason. Paths never contain keys from the app's data. */
  dropped?: Record<string, DroppedReason>
  /** Fields shortened, or left out for size (those are then absent). */
  truncated?: string[]
}

/** From `capture()` outside an instrumented call. */
export interface TurnEvent extends EventBase, TurnFields {
  kind: 'turn'
  toolName?: string
  /** The call this turn belongs to, when sent after it. */
  callId?: string
}

/** One limited-diagnostics category, when `identify` failed. */
export type ErrorCategory = 'thrown' | 'tool_error' | 'protocol' | 'timeout'

/** One `tools/call`, from the wrapper. */
export interface ToolCallEvent extends EventBase {
  kind: 'tool_call'
  callId: string
  /** The turn's `k/` reference, when `capture()` inside the call gave a conversation and a turnId. */
  turnRef?: string
  toolName: string
  server?: { name: string; version?: string }
  client?: { name: string; version?: string }
  protocolVersion?: string
  sessionId?: string
  sessionSource?: 'sent' | 'issued' | 'protocol'
  input?: Json
  /** MCP's result, with files reduced to their size and text shortened. */
  result?: { [key: string]: Json }
  status: 'completed' | 'error'
  error?: { message?: string; type?: string; code?: number | string; category?: ErrorCategory }
  durationMs: number
  round?: 'input_required' | 'continuation'
  task?: { id: string; status: string }
  intent?: string
  extra?: 'missing_tools'
  /** What `capture()` sent from inside the call before the tool returned. */
  agent?: TurnFields
  /** `identify` failed: only the fields every event needs are kept. */
  identity?: 'failed'
}

export type LSquareEvent = TurnEvent | ToolCallEvent

/** Gets every event before it's redacted and sent; `kind` tells the two shapes apart. */
export type BeforeSend = (
  event: LSquareEvent,
  context: { signal: AbortSignal },
) => LSquareEvent | null | Promise<LSquareEvent | null>

export interface Logger {
  info(message: string): void
  warn(message: string): void
}

export interface LSquareOptions {
  /** LSquare project API key. Without it (and outside dry run), the client is disabled. */
  apiKey?: string | undefined
  /** Base URL of the ingestion service. Defaults to `LSQUARE_ENDPOINT`, then https://ingest.lsquare.io. */
  endpoint?: string | undefined
  /** Run the whole pipeline but send nothing. Defaults to `LSQUARE_DRY_RUN=1`. */
  dryRun?: boolean | undefined
  /** How long `flush()` waits by default. `0`: it doesn't wait. */
  flushTimeoutMs?: number | undefined
  /** Redact more, or drop an event by returning `null`. Runs before built-in redaction. */
  beforeSend?: BeforeSend | undefined
  /** Hook deadline, counted from `capture()`. Defaults to `flushTimeoutMs`, or 2000 when that is `0`. */
  beforeSendTimeoutMs?: number | undefined
  maxConcurrentHooks?: number | undefined
  /** Requests to LSquare in flight at once (4 by default). Other events wait, and go in batches. */
  maxConcurrentRequests?: number | undefined
  /** `false` turns built-in redaction off; `{ ip: false }` turns off one pattern. */
  redact?: false | Partial<Record<PatternName, boolean>> | undefined
  /**
   * More key names whose values are replaced whole, as for `password`: matched lowercased, stripped
   * to `a-z0-9`, by their end, so `name` also covers `firstName` (and `fileName`). An array, a Set or
   * any iterable of names; one string is one name. Anything else disables capture.
   */
  redactKeys?: Iterable<string> | undefined
  onError?: ((error: LSquareError, event?: LSquareEvent) => void) | undefined
  /** Defaults to standard error where the runtime has one, so stdio servers' stdout stays clean. */
  logger?: Logger | undefined

  // Limits, mostly for tests.
  maxQueueEvents?: number | undefined
  maxQueueBytes?: number | undefined
  maxEventBytes?: number | undefined
  maxAttempts?: number | undefined
  requestTimeoutMs?: number | undefined
  fetch?: typeof fetch | undefined
}

export interface FlushResult {
  sent: number
  /** Completed without sending: dry run, or `beforeSend` returned `null`. */
  skipped: number
  failed: number
  pending: number
}
