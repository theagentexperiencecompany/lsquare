// The SDKs' contract with LSquare's ingestion: the wire limits, the checks an event must pass,
// turn references, and the acknowledgement. The shared cases (cases/) are the rest: what an SDK
// makes of a given input. The ingestion builds on these; the SDKs' tests check against them.
export type { Acknowledgement, Rejection } from './ack.js'
export {
  MAX_ACK_BYTES,
  MAX_BODY_BYTES,
  MAX_EVENT_BYTES,
  MAX_EVENT_DEPTH,
  MAX_EVENT_ID_BYTES,
  MAX_EVENTS,
  MAX_ID_BYTES,
  RETRY_AFTER_SECONDS,
} from './limits.js'
export { type TurnKey, turnRef } from './turn-ref.js'
export {
  byteLength,
  type DroppedReason,
  isTurnRef,
  type PatternName,
  type RejectionReason,
  type V1Turn,
  type V2ToolCall,
  type V2Turn,
  type Validation,
  validateEvent,
  type WireEvent,
} from './validate.js'
