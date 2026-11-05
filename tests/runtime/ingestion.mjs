// A fake ingestion, given to the client as its fetch: it keeps each batch's events and acknowledges them.
export function ingestion() {
  const events = []
  const fetch = async (_url, init) => {
    const batch = JSON.parse(String(init.body)).events
    events.push(...batch)
    return new Response(JSON.stringify({ accepted: batch.map((event) => event.eventId), duplicates: [], rejected: [] }), { headers: { 'content-type': 'application/json' } })
  }
  return { events, fetch }
}

export const runtime = `node/${process.versions.node.split('.')[0]}`
export const text = (value) => ({ content: [{ type: 'text', text: value }] })
