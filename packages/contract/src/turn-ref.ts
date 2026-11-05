/** What a turn's reference is made from. A missing org is null; a numeric turnId is its decimal string. */
export type TurnKey = readonly [orgId: string | null, conversationId: string, turnId: string]

/**
 * A turn's reference: `k/` and the SHA-256 of the key as compact JSON, in lowercase hex. The 0.2
 * SDKs make a turn's event ID the same way, so a 0.1 turn, a 0.2 turn and a call that holds the
 * turn all have the same reference.
 */
export async function turnRef(key: TurnKey): Promise<string> {
  return `k/${await sha256Hex(JSON.stringify(key))}`
}

/** SHA-256 of the text's UTF-8 bytes, in lowercase hex. Web Crypto: in Node 20+ and Workers alike. */
async function sha256Hex(text: string): Promise<string> {
  const digest = await globalThis.crypto.subtle.digest('SHA-256', new TextEncoder().encode(text))
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, '0')).join('')
}
