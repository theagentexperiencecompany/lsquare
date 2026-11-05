export type Json = string | number | boolean | null | Json[] | { [key: string]: Json }

/** Built-in redaction patterns. */
export type PatternName = 'email' | 'phone' | 'card' | 'secret' | 'ip'

/** Why a value was removed from an event. Recorded in `TurnEvent.dropped`. */
export type DroppedReason = 'wrong_type' | 'not_json' | 'missing_name' | 'unsafe_integer' | 'empty' | 'too_large'

/** Why an event was dropped, failed or is still pending. Passed to `onError`. */
export type ErrorCode =
  | 'missing_required'
  | 'invalid_conversation_id'
  | 'too_large'
  | 'queue_full'
  | 'hook_failed'
  | 'hook_timeout'
  | 'redaction_failed'
  | 'send_failed'
  | 'rejected'
  | 'pending_after_flush'
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
  /** Reports dropped by the rate limit since the last one delivered. */
  suppressed?: number
}

/**
 * What `capture()` accepts. Fields whose shape varies between apps take any value: anything the SDK
 * can't use is removed at runtime and recorded in `dropped`, never a compile error.
 */
export interface CaptureInput {
  conversationId: string
  orgId: string
  toolName: string
  question: string
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

/** The event the SDK sends. */
export interface TurnEvent {
  eventId: string
  capturedAt: string
  source: 'live' | 'backfill'
  schemaVersion: 1
  sdk: { name: string; version: string }

  conversationId: string
  turnId?: string
  orgId: string
  toolName: string

  question: string
  answer?: string
  status: 'completed' | 'error'
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

  metadata?: Record<string, string | number | boolean | null>

  /** Replacements made by built-in redaction, per pattern. */
  redactions?: Partial<Record<PatternName, number>>
  /** Values the SDK removed: path → reason. Paths never contain keys from the app's data. */
  dropped?: Record<string, DroppedReason>
}

export type BeforeSend = (
  event: TurnEvent,
  context: { signal: AbortSignal },
) => TurnEvent | null | Promise<TurnEvent | null>

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
  /** Requests to LSquare in flight at once. Other events wait their turn. */
  maxConcurrentRequests?: number | undefined
  /** `false` turns built-in redaction off; `{ ip: false }` turns off one pattern. */
  redact?: false | Partial<Record<PatternName, boolean>> | undefined
  onError?: ((error: LSquareError, event?: TurnEvent) => void) | undefined
  /** Receives dry-run lines and the default error logs. Defaults to `console`, with both levels on standard error. */
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
