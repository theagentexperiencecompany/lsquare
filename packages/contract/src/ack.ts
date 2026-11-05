import type { RejectionReason } from './validate.js'

/**
 * An event that wasn't stored, and why: by its ID, or by its position in the request when it has
 * no usable one. `resetsAt` only on `quota_exceeded`: when the daily limit resets, the next
 * 00:00 UTC as `toISOString` writes it.
 */
export type Rejection =
  | { eventId: string; reason: RejectionReason; resetsAt?: string }
  | { index: number; reason: RejectionReason; resetsAt?: string }

/**
 * The answer to a request whose events were read, sent once they are committed. Every event ID
 * appears exactly once: `accepted` if any of its copies stored something, `duplicates` if it had
 * a valid copy but nothing was stored (already stored, or not stored on purpose, as for a deleted
 * org or person), `rejected` with its first copy's reason if none was valid. Events without a
 * usable ID are rejected by index. Past a daily limit nothing is stored: every event is rejected
 * as `quota_exceeded`, valid or not, except valid ones an earlier copy of the request already
 * stored, which are duplicates.
 */
export interface Acknowledgement {
  accepted: string[]
  duplicates: string[]
  rejected: Rejection[]
}
