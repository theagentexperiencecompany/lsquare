// The core, packed and installed alone, on this Node version.
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { createRequire } from 'node:module'
import { dirname } from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { LSquare } from 'lsquare'
import { ingestion, runtime } from './ingestion.mjs'

const client = (fake) => new LSquare({ apiKey: 'lsq_test', endpoint: 'https://ingest.test', fetch: fake.fetch, logger: { info() {}, warn() {} } })

test('a capture() goes as a turn, naming this runtime', async () => {
  const fake = ingestion()
  const lsquare = client(fake)
  lsquare.capture({ conversationId: 'c1', turnId: 7, question: 'Where is my order?', answer: 'On its way.' })
  const result = await lsquare.flush()
  assert.equal(result.sent, 1)
  assert.equal(fake.events.length, 1)
  const [turn] = fake.events
  assert.equal(turn.kind, 'turn')
  assert.equal(turn.conversationId, 'c1')
  assert.equal(turn.sdk.name, 'lsquare')
  assert.equal(turn.sdk.runtime, runtime)
  assert.match(turn.eventId, /^k\/[0-9a-f]{64}$/)
})

test('the CommonJS build works too', async () => {
  const { LSquare: Required } = createRequire(import.meta.url)('lsquare')
  const fake = ingestion()
  const lsquare = new Required({ apiKey: 'lsq_test', endpoint: 'https://ingest.test', fetch: fake.fetch, logger: { info() {}, warn() {} } })
  lsquare.capture({ question: 'From require' })
  await lsquare.flush()
  assert.equal(fake.events[0].question, 'From require')
})

test('strings are redacted and shortened', async () => {
  const fake = ingestion()
  const lsquare = client(fake)
  // Prose, which has spaces: a long run of base64 characters would be kept as a file's size.
  lsquare.capture({ question: 'Write to ana@example.com', answer: 'The order is late. '.repeat(1_000), metadata: { password: 'hunter2' } })
  await lsquare.flush()
  const [turn] = fake.events
  assert.equal(turn.question, 'Write to [EMAIL_1]')
  assert.match(turn.answer, /^(The order is late\. )+.*…\[\+\d+ chars\]$/)
  assert.ok(turn.answer.length < 19_000)
  assert.deepEqual(turn.truncated, ['answer'])
  assert.match(turn.metadata.password, /^\[SECRET_\d+\]$/)
})

/** A child that writes five lines to stderr, once its parent has closed stderr's pipe: what happens to it. */
async function closedStderr(mode) {
  const code = `
    import { LSquare } from 'lsquare'
    let heard = 0
    if (process.env.MODE === 'listener') process.stderr.on('error', () => heard++)
    // Each client without a key warns once, through the default logger.
    const line = process.env.MODE === 'raw' ? () => process.stderr.write('[lsquare] a line\\n') : () => new LSquare({})
    process.stdin.once('data', () => {
      let n = 0
      const next = () => {
        if (n++ < 5) {
          line()
          setTimeout(next, 5)
          return
        }
        // Past the second for which the SDK listens after a failed write.
        setTimeout(() => {
          process.stdout.write(JSON.stringify({ listeners: process.stderr.listenerCount('error'), heard }))
          process.exit(0)
        }, 1_300)
      }
      next()
    })
  `
  const child = spawn(process.execPath, ['--input-type=module', '-e', code], { cwd: dirname(fileURLToPath(import.meta.url)), env: { ...process.env, MODE: mode } })
  // Heard from the start, so a child that fails early is seen too; killed if it hangs.
  const exit = new Promise((resolve) => {
    child.on('exit', resolve)
    child.on('error', () => resolve('spawn failed'))
  })
  const kill = setTimeout(() => child.kill(), 10_000)
  let out = ''
  child.stdout.on('data', (chunk) => (out += chunk))
  child.stderr.destroy()
  await Promise.race([new Promise((resolve) => setTimeout(resolve, 200)), exit])
  child.stdin.on('error', () => {})
  child.stdin.end('go\n')
  const status = await exit
  clearTimeout(kill)
  return { exit: status, out }
}

test("the default logger keeps the app alive once stderr's reader has gone, and leaves its listeners as they were", async () => {
  // The same writes straight to process.stderr: the 'error' nobody hears (EPIPE) crashes the process.
  assert.notEqual((await closedStderr('raw')).exit, 0)
  const sdk = await closedStderr('sdk')
  assert.equal(sdk.exit, 0)
  assert.deepEqual(JSON.parse(sdk.out), { listeners: 0, heard: 0 })
  // The app's own listener stays the only one, and hears the errors.
  const listener = await closedStderr('listener')
  assert.equal(listener.exit, 0)
  const seen = JSON.parse(listener.out)
  assert.equal(seen.listeners, 1)
  assert.ok(seen.heard >= 1)
})
