// A fake ingestion, given to the client as its fetch: it keeps each batch and acknowledges it at
// once, or holds the answer until a test releases it, so a test can tell sending that was handed
// to waitUntil from sending that merely happened to finish.
export interface Batch {
  events: Array<Record<string, any>>
  answered: boolean
}

export class Ingestion {
  readonly batches: Batch[] = []
  #held = false
  #waiting: Array<{ batch: Batch; resolve: () => void }> = []

  readonly fetch = async (_url: unknown, init?: RequestInit): Promise<Response> => {
    const events = JSON.parse(String(init?.body)).events as Batch['events']
    const batch: Batch = { events, answered: false }
    this.batches.push(batch)
    if (this.#held) await new Promise<void>((resolve) => this.#waiting.push({ batch, resolve }))
    batch.answered = true
    return new Response(JSON.stringify({ accepted: events.map((event) => event.eventId), duplicates: [], rejected: [] }), {
      headers: { 'content-type': 'application/json' },
    })
  }

  hold(): void {
    this.#held = true
  }

  release(): void {
    this.#held = false
    for (const { resolve } of this.#waiting.splice(0)) resolve()
  }

  /** Answers the held batches `which` picks, still holding the rest and later ones. */
  releaseWhere(which: (batch: Batch) => boolean): void {
    const picked = this.#waiting.filter((waiting) => which(waiting.batch))
    this.#waiting = this.#waiting.filter((waiting) => !which(waiting.batch))
    for (const { resolve } of picked) resolve()
  }

  events(kind?: 'tool_call' | 'turn'): Array<Record<string, any>> {
    return this.batches.flatMap((batch) => batch.events).filter((event) => kind === undefined || event.kind === kind)
  }

  clear(): void {
    this.batches.length = 0
  }
}

/** Waits until `check` holds, at most `ms`. */
export async function until(check: () => boolean, ms = 2_000): Promise<void> {
  const deadline = Date.now() + ms
  while (!check()) {
    if (Date.now() > deadline) throw new Error('Timed out waiting.')
    await new Promise((resolve) => setTimeout(resolve, 10))
  }
}

export const text = (value: string) => ({ content: [{ type: 'text' as const, text: value }] })
export const LINE = /^Task ID: (ses_[A-Za-z0-9_-]{22})\. Send it as session_id on later calls for this task, and pass it to any subagent working on it\.$/
export const issued = (reply: any): string | undefined => {
  const last = reply?.content?.at?.(-1)
  return last?.type === 'text' ? LINE.exec(last.text)?.[1] : undefined
}
