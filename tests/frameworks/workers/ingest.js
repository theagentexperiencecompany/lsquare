// LSquare's ingestion as a Worker's own fetch reaches it: the main Worker's outbound service in
// vitest.workers.config.ts, so clients built without a fetch option send through the default
// fetch. It keeps each batch and acknowledges it, after a delay (for the next `slow` batches, or
// all), with failures (with a Retry-After if given) or with redirects, as a test sets through
// /test/set, which also clears what it kept.
let delayMs = 0
let slow = 0
let failures = 0
let status = 503
let retryAfter = null
let redirects = 0
const batches = []
/** Requests that reached the place a redirect pointed to: there must be none. */
let followed = 0

export default {
  async fetch(request) {
    const url = new URL(request.url)
    if (url.pathname === '/v1/turns') {
      const { events } = await request.json()
      const batch = { events, answered: false }
      batches.push(batch)
      if (redirects > 0) {
        redirects--
        return new Response(null, { status: 307, headers: { location: 'https://ingest.test/test/elsewhere' } })
      }
      if (failures > 0) {
        failures--
        return new Response('failing', { status, headers: retryAfter === null ? {} : { 'retry-after': retryAfter } })
      }
      if (delayMs > 0 && slow > 0) {
        slow--
        await new Promise((resolve) => setTimeout(resolve, delayMs))
      }
      batch.answered = true
      return Response.json({ accepted: events.map((event) => event.eventId), duplicates: [], rejected: [] })
    }
    if (url.pathname === '/test/elsewhere') {
      followed++
      return Response.json({ accepted: [], duplicates: [], rejected: [] })
    }
    if (url.pathname === '/test/batches') return Response.json(batches)
    if (url.pathname === '/test/followed') return Response.json(followed)
    if (url.pathname === '/test/set') {
      delayMs = Number(url.searchParams.get('delay') ?? 0)
      slow = Number(url.searchParams.get('slow') ?? Infinity)
      failures = Number(url.searchParams.get('fail') ?? 0)
      status = Number(url.searchParams.get('status') ?? 503)
      retryAfter = url.searchParams.get('retryAfter')
      redirects = Number(url.searchParams.get('redirect') ?? 0)
      followed = 0
      batches.length = 0
      return new Response('set')
    }
    return new Response('Not Found', { status: 404 })
  },
}
