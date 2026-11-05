// What the SDK shares with the ingestion's contract (packages/contract): the same limits and names,
// so an event the SDK sends is never refused for one of them.
import { describe, expect, it } from 'vitest'
import { MAX_EVENT_ID_BYTES, MAX_ID_BYTES as CONTRACT_MAX_ID_BYTES } from '../../contract/src/limits.js'
import type { DroppedReason as ContractDroppedReason, PatternName as ContractPatternName } from '../../contract/src/validate.js'
import { MAX_ID_BYTES } from '../src/normalize.js'
import type { DroppedReason, PatternName } from '../src/types.js'

/** True only when both types have exactly the same members. */
type Same<A, B> = [A] extends [B] ? ([B] extends [A] ? true : false) : false

describe('limits shared with the contract', () => {
  it('uses the same ID limit, which is also the event ID limit', () => {
    expect(MAX_ID_BYTES).toBe(CONTRACT_MAX_ID_BYTES)
    expect(MAX_ID_BYTES).toBe(MAX_EVENT_ID_BYTES)
  })
})

describe('names shared with the contract', () => {
  it('counts redactions and reports dropped fields with the names the contract accepts', () => {
    // Checked by `npm run typecheck`: these fail to compile if a name is in one but not the other.
    const same: [Same<PatternName, ContractPatternName>, Same<DroppedReason, ContractDroppedReason>] = [true, true]
    expect(same).toEqual([true, true])
  })
})
