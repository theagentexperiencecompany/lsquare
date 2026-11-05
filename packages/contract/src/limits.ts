/** Request body, as sent. */
export const MAX_BODY_BYTES = 1024 * 1024
export const MAX_EVENTS = 100
/** One event's JSON. The SDK drops larger events before sending. */
export const MAX_EVENT_BYTES = 256 * 1024
/**
 * Nesting of one event's JSON, the event itself being level 1. The SDKs send at most 67 (0.1 keeps
 * values 64 levels deep), and the stored events must stay readable by JSON tools with their own
 * nesting limits.
 */
export const MAX_EVENT_DEPTH = 100
/** Same limit as the SDK's: a longer `${conversationId}/t/${turnId}` is hashed there. */
export const MAX_EVENT_ID_BYTES = 512
/** Org, person, conversation, tool, call and session IDs. A turnId has no limit: it's only hashed. */
export const MAX_ID_BYTES = 512
/** The SDK reads at most 64 KB of an acknowledgement. */
export const MAX_ACK_BYTES = 60 * 1024
/** The SDK honours Retry-After up to 30 s, but a short-lived process may not wait that long. */
export const RETRY_AFTER_SECONDS = 10
