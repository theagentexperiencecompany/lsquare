// Values under secret key names in JSON held in a string. What they become is in the shared cases
// (packages/contract/cases/redaction.json); this is what those can't say: that the work is linear,
// that every span is sound on any input, and that @lsquare/node has the same scanner.
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { Redactor, resolvePatterns } from '../src/redact.js'
import { isValidJson, SCAN_WORK, type ScanWork, secretSpans } from '../src/secret-spans.js'

const isSecret = (key: string) => key.toLowerCase().endsWith('password')
const unlimited = (): ScanWork => ({ left: Number.POSITIVE_INFINITY })

function time(text: string): number {
  let best = Number.POSITIVE_INFINITY
  for (let i = 0; i < 3; i++) {
    const started = performance.now()
    secretSpans(text, isSecret, unlimited())
    best = Math.min(best, performance.now() - started)
  }
  return best
}

/** Encodes `text` as a JSON string `times` times over, as a string inside a string would be. */
function encoded(text: string, times: number): string {
  let out = text
  for (let i = 0; i < times; i++) out = `{"a":${JSON.stringify(out)}}`
  return out
}

/** Texts and the work each takes: the same numbers are in secret_spans_test.py. */
const WORK_SAMPLES: [string, number][] = [
  ['{"password":"x"}', 4],
  ['{"a":"b","c":"d\\n"}', 14],
  ['{"a":"{\\"password\\":\\"x\\"}"}', 17],
  ['\'{\\"a\\": 1}\'', 14],
  ['plain "quoted": text', 3],
  ['"password":["a",{"b":1}]', 9],
  ['\\\\"x":\\\\"x":\\\\"x":', 39],
  ['"a\\q":1', 10],
]

const JSON_SAMPLES = [
  '',
  ' ',
  'null',
  'nul',
  'true',
  'truex',
  ' false ',
  '0',
  '-0',
  '01',
  '-',
  '1.',
  '1.5',
  '.5',
  '1e5',
  '1E+5',
  '1e',
  '+1',
  'NaN',
  'Infinity',
  '""',
  '"',
  '"a',
  '"\\"',
  '"\\\\"',
  '"\\n"',
  '"\\x"',
  '"\\u12"',
  '"\\u1234"',
  '"\\ud800"',
  '"\n"',
  '"\t"',
  '"\u007f"',
  '"\ud800"',
  '[]',
  '[ ]',
  '[1]',
  '[1,]',
  '[,1]',
  '[1 2]',
  '[1,2',
  '{}',
  '{ }',
  '{"a":1}',
  '{"a":1,}',
  '{"a"}',
  '{"a":}',
  '{a:1}',
  '{1:1}',
  '{"a":1 "b":2}',
  '{"a":1]',
  '[1}',
  '{}{}',
  '[] x',
  '\f[]',
  '\u00a0[]',
  '\ufeff[]',
  '[\n1\r,\t2 ]',
  `[${'1'.repeat(5000)}]`,
  '{"a":[{"b":[{"c":null}]}],"d":"e"}',
  '{a":1}',
  '"\u0000"',
  '"\u001f"',
  '"\\n\t"',
  '"\\uzzzz"',
  '"\\uABCD"',
  '"\\uabcd"',
  '"\\u123"',
  '"\\u\u0661\u0662\u0663\u0664"',
  '"\\/\\b\\f\\r\\t"',
  '"\\ "',
  '{]',
  '[}',
  '[[1] ]',
  '{"a"x1}',
  '{"a" :1}',
  '1e-5',
]

describe('secretSpans', () => {
  it.each([
    ['quotes', (n: number) => '"'.repeat(n)],
    ['backslashes', (n: number) => '\\'.repeat(n)],
    ['escaped quotes', (n: number) => '\\"'.repeat(n)],
    ['a quote, then backslashes', (n: number) => `"${'\\'.repeat(n)}`],
    ['one backslash at the end', (n: number) => `${'"a"'.repeat(n)}\\`],
    ['strings holding quotes', (n: number) => '"\\"",'.repeat(n)],
    ['keys', (n: number) => '"a":'.repeat(n)],
    ['secret keys', (n: number) => '"password":'.repeat(n)],
    ['secret values', (n: number) => '"password":"x",'.repeat(n)],
    ['open brackets under a secret key', (n: number) => `"password":${'['.repeat(n)}`],
    ['open brackets, many times', (n: number) => '"password":['.repeat(n)],
    ['a long payload encoded four times', (n: number) => encoded(`{"password":"${'x'.repeat(n)}"}`, 4)],
    ['a long payload encoded six times', (n: number) => encoded(`{"a":"${'x'.repeat(n)}"}`, 6)],
    ['printed keys', (n: number) => '\\\\"x":'.repeat(n)],
    ['keys full of escapes', (n: number) => '"\\\\\\\\\\\\\\\\x":'.repeat(n)],
    ['escapes surviving every decoding', (n: number) => `${'\\'.repeat(16 * n)}x`],
    ['commas after a quote', (n: number) => `"password":"${'",'.repeat(n)}`],
    ['keys after a question mark', (n: number) => `[${'1,'.repeat(n)}${'?"password":1,'.repeat(n / 100)}`],
    ['blank lines in a value holding a quote', (n: number) => `"password":"a" b${'\n'.repeat(n)}c`],
    ['long keys', (n: number) => `{"${'x'.repeat(n)}":1,"${'y'.repeat(n)}":2}`],
    ['quotes past the nesting limit', (n: number) => encoded(`"${' '.repeat(n)}`, 5)],
    ['backslashes past the nesting limit', (n: number) => encoded(`"${'\\'.repeat(n)}`, 5)],
  ])('is linear on %s', (_name, build) => {
    // A few tries: the heavier test files run alongside and can slow one measurement.
    let ratio = Number.POSITIVE_INFINITY
    for (let attempt = 0; attempt < 5 && ratio >= 10; attempt++) {
      ratio = Math.min(ratio, time(build(40_000)) / Math.max(time(build(10_000)), 0.5))
    }
    expect(ratio).toBeLessThan(10)
  })

  it('gives spans in order, not overlapping, inside the string, on any input', () => {
    const pieces = ['"', '\\', '\\"', '\\\\', '\\u0022', '\\n', ':', ',', '{', '}', '[', ']', 'password', '"password":', 'abc', '12', 'true', ' ', '\n', '😀']
    let seed = 7
    const next = (limit: number) => {
      seed = (seed * 1_103_515_245 + 12_345) % 2_147_483_648
      return seed % limit
    }
    for (let round = 0; round < 3000; round++) {
      const text = Array.from({ length: next(30) }, () => pieces[next(pieces.length)]).join('')
      let at = 0
      for (const span of secretSpans(text, isSecret, unlimited())) {
        expect(span.start, text).toBeGreaterThanOrEqual(at)
        expect(span.end, text).toBeGreaterThan(span.start)
        expect(span.end, text).toBeLessThanOrEqual(text.length)
        at = span.end
      }
    }
  })

  it('leaves a string with no key alone, and the same object comes back', () => {
    const redactor = new Redactor(resolvePatterns(undefined))
    for (const text of ['no quotes: here', '"no colon"', 'C:\\Users\\me', '{"a":"b"}']) expect(redactor.string(text)).toBe(text)
  })

  it('counts its work as the Python SDK does', () => {
    const used = WORK_SAMPLES.map(([text]) => {
      const work: ScanWork = { left: SCAN_WORK }
      secretSpans(text as string, isSecret, work)
      return SCAN_WORK - work.left
    })
    expect(used).toEqual(WORK_SAMPLES.map(([, count]) => count))
  })

  it('reads to the last unit of its work, and replaces the string whole past it', () => {
    expect(SCAN_WORK).toBe(1_000_000)
    for (const [text, count] of WORK_SAMPLES) {
      const read = secretSpans(text, isSecret, unlimited())
      expect(secretSpans(text, isSecret, { left: count })).toEqual(read)
      expect(secretSpans(text, isSecret, { left: count - 1 })).toEqual([{ start: 0, end: text.length, identity: undefined, quote: '' }])
    }
  })

  it('replaces a string whole once the event has used its work, and sends nothing unread', () => {
    const redactor = new Redactor(['secret'])
    const crafted = `":"{\\{${'\\'.repeat(7)}`.repeat(20_000)
    expect(redactor.string(crafted)).toBe('[SECRET_1]')
    expect(redactor.string('{"note":"not a secret"}')).toBe('[SECRET_2]')
    expect(redactor.string('no quote and colon here')).toBe('no quote and colon here')
    expect(redactor.counts).toEqual({ secret: 2 })
  })

  it('has work to spare on the largest ordinary event', () => {
    const value = (index: number) => `value "${index}"\n`
    const nested = JSON.stringify({ a: JSON.stringify(Object.fromEntries(Array.from({ length: 2000 }, (_, i) => [`k${i}`, value(i)]))) })
    const work: ScanWork = { left: SCAN_WORK }
    const times = Math.ceil(1_000_000 / nested.length)
    for (let i = 0; i < times; i++) secretSpans(nested, isSecret, work)
    expect(work.left).toBeGreaterThan(0)
  })

  it('is the same file in @lsquare/node', () => {
    const read = (path: string) => readFileSync(new URL(path, import.meta.url), 'utf8')
    expect(read('../../node/src/secret-spans.ts')).toBe(read('../src/secret-spans.ts'))
  })
})

describe('isValidJson', () => {
  const parses = (text: string) => {
    try {
      JSON.parse(text)
      return true
    } catch {
      return false
    }
  }

  it.each(JSON_SAMPLES)('agrees with JSON.parse on %j', (text) => {
    expect(isValidJson(text, unlimited())).toBe(parses(text))
  })

  it('agrees with JSON.parse on inputs nobody chose', () => {
    const pieces = ['{', '}', '[', ']', ':', ',', '"', '\\', '\\n', '\\u12', '\\u1234', 'a', '1', '-', '0', '.', 'e', 'E', '+', 'true', 'false', 'null', 'NaN', ' ', '\n', '\t', '\f', '\u0001', '\u007f', '\ud800', '😀', '""', '{}', '[]', '"a":', '"b"', '1e5', '01', '-0', '1.', '.5']
    let seed = 11
    const next = (limit: number) => {
      seed = (seed * 1_103_515_245 + 12_345) % 2_147_483_648
      return (seed >> 8) % limit
    }
    let valid = 0
    for (let round = 0; round < 40_000; round++) {
      const text = Array.from({ length: next(9) }, () => pieces[next(pieces.length)]).join('')
      expect(isValidJson(text, unlimited()), text).toBe(parses(text))
      if (parses(text)) valid++
    }
    expect(valid).toBeGreaterThan(500)
  })

  it('takes any depth', () => {
    expect(isValidJson(`${'['.repeat(200_000)}${']'.repeat(200_000)}`, unlimited())).toBe(true)
    expect(isValidJson(`${'{"a":'.repeat(100_000)}1${'}'.repeat(100_000)}`, unlimited())).toBe(true)
    expect(isValidJson('['.repeat(200_000), unlimited())).toBe(false)
  })
})
