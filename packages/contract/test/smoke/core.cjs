// The smoke check of the built @lsquare/contract, on each Node version it supports (run.sh, in CI's
// core job): the build loaded is the one its exports name, an event passes validateEvent, and
// turnRef gives Node's own SHA-256 of the key. Usage: from esm.mjs or cjs.cjs, in a directory where
// the package is installed.
const assert = require('node:assert/strict')
const { createHash } = require('node:crypto')
const { basename } = require('node:path')

module.exports = async function smoke(contract, build, resolved) {
  assert.equal(basename(resolved), build === 'esm' ? 'index.js' : 'index.cjs', 'the build its exports name')
  const event = {
    schemaVersion: 1,
    eventId: 'smoke-1',
    source: 'live',
    capturedAt: '2026-11-01T00:00:00.000Z',
    orgId: 'o',
    conversationId: 'c',
    turnId: 't',
    toolName: 'search',
    question: 'q',
  }
  assert.deepEqual(contract.validateEvent(event), { ok: true, event, json: JSON.stringify(event) })
  const key = ['o', 'c', 't']
  assert.equal(await contract.turnRef(key), `k/${createHash('sha256').update(JSON.stringify(key)).digest('hex')}`)
  console.log(`${build} (${basename(resolved)}): validateEvent and turnRef pass`)
}
