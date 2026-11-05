import { execFileSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { describe, expect, it, vi } from 'vitest'
import { PATTERNS as V1_PATTERNS } from '../../node/src/redact.js'
import { buildCall, buildTurn, type CallRecord } from '../src/event.js'
import { PATTERNS, readKeys, Redactor, resolveKeys, resolvePatterns, TEXT_FIELD } from '../src/redact.js'
import { binarySize, CUT_MARK, LOOKAHEAD, newState, SCAN_BUDGET, shapeJson, shapeString } from '../src/shape.js'
import type { ToolCallEvent, TurnEvent } from '../src/types.js'
import { firstDifference, inputs, V1 } from './v1-patterns.js'

const all = resolvePatterns(undefined)
const context = { sdk: { name: 'lsquare', version: '0.2.0', language: 'js' as const }, patterns: all, redactKeys: [] as string[], maxEventBytes: 256 * 1024 }
/** Prose: spaces keep it from looking like base64. */
const prose = (chars: number) => 'lorem ipsum '.repeat(Math.ceil(chars / 12)).slice(0, chars)

function turn(input: Record<string, unknown>, extra: Partial<typeof context> = {}): TurnEvent {
  const built = buildTurn(input, '2026-10-28T00:00:00.000Z', { ...context, ...extra }, false)
  if (!built.ok) throw new Error(built.code)
  return built.draft.event as TurnEvent
}

function call(over: Partial<CallRecord> = {}, extra: Partial<typeof context> = {}): ToolCallEvent {
  const record: CallRecord = {
    callId: 'b3c2f0a4-1111-4111-8111-000000000001',
    capturedAt: '2026-10-28T00:00:00.000Z',
    toolName: 'search_orders',
    status: 'completed',
    durationMs: 12,
    ids: {},
    diagnostics: false,
    ...over,
  }
  const built = buildCall(record, { ...context, ...extra }, false)
  if (!built.ok) throw new Error(built.code)
  return built.draft.event as ToolCallEvent
}

describe('cutting strings', () => {
  it('cuts at 16,000 code points in a turn, ending with the exact count of what was cut', () => {
    const event = turn({ answer: prose(20_000) })
    expect(event.answer).toBe(`${prose(16_000)}…[+4000 chars]`)
    expect(event.truncated).toEqual(['answer'])
  })

  it('counts an emoji as one character and never splits one', () => {
    const text = '😀'.repeat(16_001)
    const event = turn({ answer: text })
    expect(event.answer).toBe(`${'😀'.repeat(16_000)}…[+1 chars]`)
  })

  it('leaves a string at the limit alone, and marks nothing', () => {
    const event = turn({ answer: prose(16_000) })
    expect(event.answer).toBe(prose(16_000))
    expect(event.truncated).toBeUndefined()
  })

  it('cuts results at 4,000, inputs at 16,000 and the intent at 500', () => {
    const event = call({
      input: { value: { q: prose(17_000) } },
      result: { value: { content: [{ type: 'text', text: prose(5_000) }], structuredContent: { rows: [prose(4_500)] } } },
      intent: prose(600),
    })
    expect(event.input).toEqual({ q: `${prose(16_000)}…[+1000 chars]` })
    expect(event.result).toEqual({
      content: [{ type: 'text', text: `${prose(4_000)}…[+1000 chars]` }],
      structuredContent: { rows: [`${prose(4_000)}…[+500 chars]`] },
    })
    expect(event.intent).toBe(`${prose(500)}…[+100 chars]`)
    expect(event.truncated).toEqual(['intent', 'input', 'result'])
  })

  it('keeps the first 100 entries of lists and objects, and stops nesting at 10 levels', () => {
    let deep: unknown = 'bottom'
    for (let i = 0; i < 12; i++) deep = { next: deep }
    const object = Object.fromEntries(Array.from({ length: 150 }, (_, i) => [`k${i}`, i]))
    const event = call({ input: { value: { list: Array.from({ length: 150 }, (_, i) => i), object, deep } } })
    const input = event.input as { list: number[]; object: Record<string, number>; deep: unknown }
    expect(input.list).toHaveLength(100)
    expect(Object.keys(input.object)).toHaveLength(100)
    let level = input.deep as Record<string, unknown>
    // input is level 1 and deep level 2, so 8 more levels of `next` fit before the marker.
    for (let i = 0; i < 8; i++) level = level.next as Record<string, unknown>
    expect(level.next).toEqual({ omitted: 'depth' })
    expect(event.truncated).toEqual(['input'])
  })
})

describe('the scan budget', () => {
  it('ends a string with …[cut] when counting the rest would pass it, and that uses the budget up', () => {
    const state = newState()
    const cut = shapeString(prose(2_000_000), 16_000, state)
    expect(cut).toMatchObject({ cut: 16_000, mark: CUT_MARK })
    // Reading toward the end until the budget ran out is charged: the next string can't read it again.
    expect(state.scan.remaining).toBe(0)
    expect(shapeString('short', 16_000, state)).toMatchObject({ window: '', mark: CUT_MARK })
  })

  it('still reads 8,000 characters past a cut where the budget runs out, for redaction, and ends it …[cut]', () => {
    // Inside the kept part: cut where the budget ran out.
    const inside = newState({ budget: 10_000 })
    expect(shapeString(prose(100_000), 16_000, inside)).toMatchObject({ window: prose(18_000), cut: 10_000, mark: CUT_MARK })
    expect(inside.scan.remaining).toBe(0)
    // Inside what's read past a cut at the limit: all 8,000 are read, though the budget paid for 5.
    const ahead = newState({ budget: 16_005 })
    expect(shapeString(prose(100_000), 16_000, ahead)).toMatchObject({ window: prose(24_000), cut: 16_000, mark: CUT_MARK })
    expect(ahead.scan.remaining).toBe(0)
    // A rest that fits is still counted exactly.
    const fits = newState({ budget: 16_005 })
    expect(shapeString(prose(16_004), 16_000, fits)).toMatchObject({ cut: 16_000, mark: '…[+4 chars]' })
    expect(fits.scan.remaining).toBe(1)
  })

  it('reads at most 8,000 characters more than the budget, where it runs out inside a string', () => {
    const spy = vi.spyOn(String.prototype, 'charCodeAt')
    let reads: number
    try {
      shapeJson([prose(990_000), prose(2_000_000)], 16_000, newState())
    } finally {
      reads = spy.mock.calls.length
      spy.mockRestore()
    }
    expect(reads).toBeGreaterThan(SCAN_BUDGET)
    expect(reads).toBeLessThanOrEqual(SCAN_BUDGET + LOOKAHEAD)
  })

  it('keeps one event with 100 large strings within it: exact counts while it lasts, then …[cut]', () => {
    const strings = Array.from({ length: 100 }, () => prose(200_000))
    const state = newState()
    const started = performance.now()
    const shaped = shapeJson(strings, 16_000, state) as Array<{ cut: number; mark: string }>
    expect(performance.now() - started).toBeLessThan(200)
    expect(state.scan.remaining).toBe(0)
    // Each of the first four costs its slot and all its 200,000 characters, the rest being counted.
    expect(shaped.slice(0, 4).map((cut) => cut.mark)).toEqual(Array(4).fill('…[+184000 chars]'))
    // The fifth's rest no longer fits: counting it uses the budget up, and no entry is visited after.
    expect(shaped[4]).toMatchObject({ cut: 16_000, mark: CUT_MARK })
    expect(shaped).toHaveLength(5)
    const event = call({ input: { value: { strings } } })
    expect((event.input as { strings: string[] }).strings).toHaveLength(5)
  })

  it('bounds the work: 100 strings of 2 million characters read about a million in all', () => {
    const big = prose(2_000_000)
    const started = performance.now()
    const state = newState()
    const shaped = shapeJson(Array(100).fill(big), 16_000, state) as unknown[]
    expect(performance.now() - started).toBeLessThan(50)
    expect(shaped).toHaveLength(1)
  })

  it('shortens a 100 MB text result quickly', () => {
    const big = prose(100 * 1024 * 1024)
    const started = performance.now()
    const event = call({ result: { value: { content: [{ type: 'text', text: big }] } } })
    expect(performance.now() - started).toBeLessThan(500)
    expect((event.result as { content: { text: string }[] }).content[0]!.text).toBe(`${prose(4_000)}${CUT_MARK}`)
  })
})

describe('files sent as text', () => {
  const base64 = 'QUJD'.repeat(500)

  it('keeps base64 strings and data: URIs as their size, and leaves prose and code alone', () => {
    expect(binarySize(base64)).toBe(1_500)
    expect(binarySize(`${base64}==`)).toBe(1_500)
    expect(binarySize('data:image/png;base64,QUJD')).toBe(3)
    expect(binarySize('data:text/plain,hello')).toBeUndefined()
    expect(binarySize('QUJD'.repeat(200))).toBeUndefined()
    expect(binarySize(prose(5_000))).toBeUndefined()
    expect(binarySize(`function f() { return ${'a'.repeat(5_000)} }`)).toBeUndefined()
    // Only the ends are read: base64-looking at both, with prose in the middle, is a file too.
    expect(binarySize(`${'A'.repeat(4_096)}${prose(10_000)}${'A'.repeat(1_024)}`)).toBe(Math.floor((15_120 * 3) / 4))
  })

  it('becomes { omitted: "binary", bytes } in JSON, and [binary, N bytes] in a field that must be a string', () => {
    const event = call({
      input: { value: { file: base64 } },
      result: { value: { content: [{ type: 'text', text: base64 }, { type: 'image', data: base64, mimeType: 'image/png' }, { type: 'resource', resource: { uri: 'file:///a', blob: base64 } }] } },
    })
    expect(event.input).toEqual({ file: { omitted: 'binary', bytes: 1_500 } })
    expect(event.result).toEqual({
      content: [
        { type: 'text', text: '[binary, 1500 bytes]' },
        { type: 'image', mimeType: 'image/png', bytes: 1_500 },
        { type: 'resource', uri: 'file:///a', bytes: 1_500 },
      ],
    })
    expect(turn({ answer: base64 }).answer).toBe('[binary, 1500 bytes]')
  })

  it('keeps resource links and embedded text, never unknown block contents', () => {
    const event = call({
      result: {
        value: {
          content: [
            { type: 'resource_link', uri: 'file:///orders.csv', name: 'orders.csv', mimeType: 'text/csv', extra: 'x' },
            { type: 'resource', resource: { uri: 'file:///a.txt', mimeType: 'text/plain', text: 'hello' } },
            { type: 'widget', payload: 'anything' },
          ],
          isError: false,
          _meta: { trace: 'x' },
        },
      },
    })
    expect(event.result).toEqual({
      content: [
        { type: 'resource_link', uri: 'file:///orders.csv', name: 'orders.csv', mimeType: 'text/csv' },
        { type: 'resource', uri: 'file:///a.txt', mimeType: 'text/plain', text: 'hello' },
        { type: 'widget' },
      ],
      isError: false,
    })
  })
})

describe('redaction around a cut', () => {
  const pad = (n: number) => prose(n - 1) + ' '

  it.each([
    ['a PEM key', `-----BEGIN PRIVATE KEY-----\n${'A'.repeat(20_000)}`],
    ['a JWT', `eyJhbGciOiJIUzI1NiJ9.${'a'.repeat(30_000)}.sig`],
    ['an sk- key', `sk-${'a'.repeat(30_000)}`],
    ['a Bearer token', `Bearer ${'a1'.repeat(15_000)}`],
  ])('still masks %s cut at the limit', (_name, secret) => {
    const event = turn({ answer: pad(15_900) + secret })
    expect(event.answer).toMatch(/ \[SECRET_1\]…\[(\+\d+ chars|cut)\]$/)
    expect(event.redactions).toEqual({ secret: 1 })
  })

  // The cut falls `kept` characters into the token, where the budget runs out inside the string
  // (10,000) or inside the 8,000 read past a cut at the limit (16,005).
  it.each([
    ['a card number', '4111 1111 1111 1111', 14, '[CARD_1]', 'card'],
    ['an email', 'jane.doe@example.com', 14, '[EMAIL_1]', 'email'],
    ["a URL's credentials", 'https://alice:supersecretpassword@db.example.com/x', 20, 'https://[SECRET_1]', 'secret'],
    ['an AWS secret key', 'aws_secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY', 41, '[SECRET_1]', 'secret'],
    ['a key prefix after a hyphen', 'id-sk-abcdefghijklmnopqrstuvwxyz0123', 20, 'id-[SECRET_1]', 'secret'],
    ['an IPv6 address', 'fe80:0:0:0:202:b3ff:fe1e:8329', 20, '[IP_1]', 'ip'],
  ] as const)('masks %s running into a cut where the scan budget runs out', (_name, token, kept, masked, pattern) => {
    for (const [budget, at] of [[10_000, 10_000], [16_005, 16_000]] as const) {
      const state = newState({ budget })
      const redactor = new Redactor(all)
      const shaped = shapeString(`${pad(at - kept)}${token} ${prose(20_000)}`, 16_000, state)
      expect(redactor.finalize(shaped, TEXT_FIELD)).toBe(`${pad(at - kept)}${masked}${CUT_MARK}`)
      expect(redactor.counts).toEqual({ [pattern]: 1 })
    }
  })

  it('masks a secret that runs past the cut whole, matched with what follows it', () => {
    const key = `sk-${'k'.repeat(40)}`
    const event = turn({ answer: `${pad(15_980)}${key} after` })
    expect(event.answer).toBe(`${pad(15_980)}[SECRET_1]…[+${key.length + 6 - 20} chars]`)
  })

  it('neither replaces nor counts what comes entirely after the cut', () => {
    const event = turn({ answer: `${prose(16_000)} jane@acme.io and sk-${'k'.repeat(40)}` })
    expect(event.answer).toBe(`${prose(16_000)}…[+${1 + 12 + 5 + 43} chars]`)
    expect(event.redactions).toBeUndefined()
  })

  it('numbers a value the same in a cut string and elsewhere', () => {
    const event = turn({ question: 'is jane@acme.io there?', answer: `${pad(15_990)}jane@acme.io ${prose(1_000)}` })
    expect(event.question).toBe('is [EMAIL_1] there?')
    expect(event.answer!.startsWith(`${pad(15_990)}[EMAIL_1]`)).toBe(true)
  })

  it('applies secret key names before any cut', () => {
    const event = call({ input: { value: { password: prose(20_000) } } })
    expect(event.input).toEqual({ password: '[SECRET_1]' })
  })
})

describe('redactKeys', () => {
  it('replaces values under matching keys whole, matched by their end like the secret key names', () => {
    const redactKeys = resolveKeys(['name', 'Date-Of-Birth', '', 7, 'name'])
    expect(redactKeys).toEqual(['name', 'dateofbirth'])
    const event = call(
      {
        input: { value: { firstName: 'Jane', customer_name: { first: 'J' }, fileName: 'a.csv', dateOfBirth: '1990-01-01', city: 'Rome' } },
        result: { value: { structuredContent: { rows: [{ name: 'Bob' }] } } },
      },
      { redactKeys },
    )
    expect(event.input).toEqual({ firstName: '[SECRET_1]', customer_name: '[SECRET_2]', fileName: '[SECRET_3]', dateOfBirth: '[SECRET_4]', city: 'Rome' })
    expect(event.result).toEqual({ structuredContent: { rows: [{ name: '[SECRET_5]' }] } })
  })

  it('reads a Set or any other iterable of names, and one string as one name', () => {
    function* names() {
      yield 'name'
      yield 'Date-Of-Birth'
    }
    for (const option of [new Set(['name', 'Date-Of-Birth']), new Map([['name', 1], ['Date-Of-Birth', 2]]).keys(), names(), ['name', 'Date-Of-Birth']]) {
      expect(readKeys(option)).toEqual({ keys: ['name', 'dateofbirth'], skipped: false })
    }
    expect(readKeys('Name')).toEqual({ keys: ['name'], skipped: false })
    expect(readKeys(undefined)).toEqual({ keys: [], skipped: false })
    expect(readKeys([])).toEqual({ keys: [], skipped: false })
    // An endless one gives its first 1,000, and is let go.
    let closed = false
    function* endless() {
      try {
        for (let i = 0; ; i++) yield `k${i}`
      } finally {
        closed = true
      }
    }
    expect(readKeys(endless())?.keys).toHaveLength(1_000)
    expect(closed).toBe(true)
  })

  it("flags entries that aren't names, and refuses what isn't a list of names", () => {
    expect(readKeys(['name', 7, '', '--'])).toEqual({ keys: ['name'], skipped: true })
    // A Map gives its entries, which aren't names.
    expect(readKeys(new Map([['name', 1]]))).toEqual({ keys: [], skipped: true })
    for (const option of [{ name: true }, 42, true, () => ['name']]) expect(readKeys(option)).toBeUndefined()
  })
})

describe('patterns', () => {
  it("are 0.1's, with JavaScript's \\s written out for Python", () => {
    const ws = '\\t\\n\\v\\f\\r \\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff'
    for (const [name, pattern] of Object.entries(PATTERNS)) {
      // Rewritten, and compared by their matches below.
      if (name === 'phone' || name === 'ip') continue
      const v1 = V1_PATTERNS[name as keyof typeof V1_PATTERNS].source
      const expanded = v1.replaceAll(String.raw`[^\s`, `[^${ws}`).replaceAll(String.raw`\s`, `[${ws}]`)
      expect(pattern.source, name).toBe(expanded)
    }
    const redactor = new Redactor(all)
    expect(redactor.text('Authorization: Bearer\u00a0abcdefghijklmnop1234')).toBe('Authorization: [SECRET_1]')
  })

  it("phone and ip, rewritten, match exactly what 0.1's did", () => {
    expect(firstDifference(PATTERNS.phone, V1.phone)).toBeUndefined()
    expect(firstDifference(PATTERNS.ip, V1.ip)).toBeUndefined()
    // As many as Python's tests/v1_patterns.py compares on.
    expect([...inputs()]).toHaveLength(144_994)
  })

  it('has the phone pattern that scripts/phone-pattern.mjs builds', () => {
    const script = fileURLToPath(new URL('../scripts/phone-pattern.mjs', import.meta.url))
    expect(execFileSync(process.execPath, [script], { encoding: 'utf8' })).toBe(`${PATTERNS.phone.source}\n`)
  })
})
